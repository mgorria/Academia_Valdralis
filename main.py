import asyncio
import json
import logging
import os
import signal
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from dotenv import load_dotenv
from openai import AsyncOpenAI
from psycopg.types.json import Jsonb
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO"),
)
logger = logging.getLogger("control-partida-sandra")
logging.getLogger("httpx").setLevel(logging.WARNING)

TOKEN_NARRADOR = os.getenv("TOKEN_NARRADOR")
TOKEN_CONTROL = os.getenv("TOKEN_CONTROL")
MI_CHAT_ID = os.getenv("MI_CHAT_ID")
SANDRA_CHAT_ID = os.getenv("SANDRA_CHAT_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.5")
DATABASE_URL = os.getenv("DATABASE_URL")

DATA_FILE = Path(os.getenv("DATA_FILE", "data/data.json"))
MEMORY_MD_PATH = Path(os.getenv("MEMORY_MD_PATH", "data/memoria_actual.md"))
APP_TIMEZONE = ZoneInfo(os.getenv("APP_TIMEZONE", "Europe/Madrid"))
DAILY_SUMMARY_HOUR = int(os.getenv("DAILY_SUMMARY_HOUR", "23"))
DAILY_SUMMARY_MINUTE = int(os.getenv("DAILY_SUMMARY_MINUTE", "0"))
PRELUDE_ENABLED_DEFAULT = os.getenv("PRELUDE_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
PRELUDE_PATH = Path(os.getenv("PRELUDE_PATH", "lore/preludio.md"))
START_MESSAGE_PATH = Path(os.getenv("START_MESSAGE_PATH", "lore/inicio.md"))
PRELUDE_START_DATE = date.fromisoformat(os.getenv("PRELUDE_START_DATE", "2026-06-29"))
PRELUDE_END_DATE = date.fromisoformat(os.getenv("PRELUDE_END_DATE", "2026-07-12"))
STORY_START_DATE = date.fromisoformat(os.getenv("STORY_START_DATE", "2026-07-13"))
PRELUDE_REPLY_ENABLED = os.getenv("PRELUDE_REPLY_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
PRELUDE_HOUR = int(os.getenv("PRELUDE_HOUR", "21"))
PRELUDE_MINUTE = int(os.getenv("PRELUDE_MINUTE", "30"))
STORY_START_HOUR = int(os.getenv("STORY_START_HOUR", "0"))
STORY_START_MINUTE = int(os.getenv("STORY_START_MINUTE", "1"))
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "500"))
RECENT_HISTORY_FOR_AI = int(os.getenv("RECENT_HISTORY_FOR_AI", "24"))
MESSAGE_BUFFER_SECONDS = int(os.getenv("MESSAGE_BUFFER_SECONDS", "25"))
CHAPTER_REVIEW_PAUSE_DAYS = int(os.getenv("CHAPTER_REVIEW_PAUSE_DAYS", "14"))

LORE_PATH = Path("lore/biblia.md")
CHAPTER_TITLES = {
    1: "La carta bajo la puerta",
    2: "El tren de medianoche",
    3: "Bienvenida a Valdralis",
    4: "La Ceremonia del Umbral",
    5: "La primera clase",
    6: "La noche del pacto",
    7: "El Ala Norte",
    8: "La marca bajo la piel",
    9: "El baile de las tres invitaciones",
    10: "El Sello del Umbral",
}

control_app: Application | None = None
narrador_app: Application | None = None
stop_event: asyncio.Event | None = None
sandra_message_buffers: dict[int, list[str]] = {}
sandra_message_tasks: dict[int, asyncio.Task] = {}
DB_READY = False


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Falta la variable {name}")
    return value


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def character_sheet(
    relacion_actual: str,
    secretos_que_sabe: list[str] | None = None,
    ultima_escena_juntos: str = "Aun no aparece en escena",
    tension_romantica: str = "No aplica",
    no_revelar_todavia: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "relacion_actual": relacion_actual,
        "secretos_que_sabe": secretos_que_sabe or [],
        "ultima_escena_juntos": ultima_escena_juntos,
        "tension_romantica": tension_romantica,
        "no_revelar_todavia": no_revelar_todavia or [],
    }


def default_character_sheets() -> dict[str, dict[str, Any]]:
    return {
        "Kilnip": character_sheet(
            "Guia magico inicial de Sandra, encerrado en el sello azul de la carta hasta que ella la abra",
            secretos_que_sabe=[
                "Sabe encontrar el Bazar de los Primeros",
                "Reconoce senales practicas de Valdralis, pero no entiende todos los secretos",
            ],
            tension_romantica="No aplica; ternura, humor y compania",
            no_revelar_todavia=[
                "No puede explicar de golpe el linaje Valmorien ni el Sello del Umbral",
            ],
        ),
        "Nora": character_sheet(
            "Aun no aparece; futura primera amiga estable de Sandra",
            secretos_que_sabe=[
                "Conoce normas, profesores, pasillos utiles y formas de no llamar la atencion",
            ],
            tension_romantica="No aplica; amistad y apoyo practico",
        ),
        "Izan": character_sheet(
            "Aun no aparece; futuro aliado medium timido",
            secretos_que_sabe=[
                "Puede oir ecos en paredes y objetos, pero no siempre distingue memoria, futuro o deseo",
            ],
            tension_romantica="No aplica; vulnerabilidad y pistas inquietantes",
        ),
        "Mara": character_sheet(
            "Aun no aparece; futura amiga semilunar impulsiva y leal",
            secretos_que_sabe=[
                "Conoce parte del mundo licantropo y las versiones que la academia oculta",
            ],
            tension_romantica="No aplica; energia, proteccion y valentia",
        ),
        "Theo": character_sheet(
            "Aun no aparece; futuro aliado alquimista humano, encantador y algo desastre",
            secretos_que_sabe=[
                "Sabe improvisar herramientas, mezclas, permisos y pequenas trampas practicas",
            ],
            tension_romantica="No aplica; alivio ligero y recursos",
        ),
        "Lucien": character_sheet(
            "Aun no aparece; vampiro alumno del turno nocturno, neofito noble de Casa Veyrath",
            secretos_que_sabe=[
                "Conoce fragmentos sobre Elara por archivos de sangre, retratos y secretos de su casa",
                "Su casa heredo una deuda con Elara y con la Casa Valmorien",
            ],
            tension_romantica="Potencial alto: deseo contenido, distancia, control y culpa heredada",
            no_revelar_todavia=[
                "No revelar pronto la traicion parcial de Casa Veyrath a Elara",
                "No revelar de golpe por que la sangre Valmorien le afecta",
            ],
        ),
        "Kael": character_sheet(
            "Aun no aparece; licantropo intenso, fisico, protector y desconfiado de la autoridad",
            secretos_que_sabe=[
                "Su manada perdio miembros por culpa del Sello del Umbral",
                "Algunos licantropos creen que Sandra podria ser un riesgo",
            ],
            tension_romantica="Potencial alto: calor, proteccion, honestidad brusca y miedo a asustarla",
            no_revelar_todavia=[
                "No revelar pronto que algunos licantropos creen que matarla seria mas seguro",
            ],
        ),
        "Aurelian": character_sheet(
            "Aun no aparece; fae hermoso, ambiguo y peligroso, criado entre cortes antiguas",
            secretos_que_sabe=[
                "Sabe una ruta hacia las Criptas del Umbral",
                "Entiende pactos antiguos y precios que otros no ven",
            ],
            tension_romantica="Potencial alto: provocacion, belleza inquietante, libertad y peligro elegante",
            no_revelar_todavia=[
                "No revelar la ruta a las Criptas del Umbral sin un precio o decision fuerte",
            ],
        ),
        "Dario": character_sheet(
            "Padre cruel y controlador de Sandra; la encierra creyendo que la protege o la posee",
            secretos_que_sabe=[
                "Conocia Valdralis antes de la carta",
                "Sabe mas de Elara y del peligro de Sandra de lo que admite",
            ],
            tension_romantica="No aplica; antagonismo domestico y miedo",
            no_revelar_todavia=[
                "No revelar pronto todo lo que Dario sabe sobre Elara",
                "No convertirlo en ignorante simple; su miedo tiene informacion real detras",
            ],
        ),
        "Severin Cael": character_sheet(
            "Rector de Valdralis; autoridad elegante, severa y politica",
            secretos_que_sabe=[
                "Sabe mas de Elara de lo que admite",
                "Oculta verdades para proteger la estabilidad de Valdralis",
            ],
            tension_romantica="No aplica; presion institucional",
            no_revelar_todavia=[
                "No revelar pronto que decisiones tomo la rectoria la noche en que Elara desaparecio",
            ],
        ),
        "Mireya Noct": character_sheet(
            "Profesora de Pactos y Juramentos; protectora en apariencia, calculadora por debajo",
            secretos_que_sabe=[
                "Entiende que Sandra puede alterar pactos antiguos",
                "Puede estar vinculada directa o indirectamente a la Orden del Umbral",
            ],
            tension_romantica="No aplica; mentora ambigua y posible amenaza",
            no_revelar_todavia=[
                "No confirmar pronto si sirve a la Orden del Umbral",
            ],
        ),
        "Octavian Rook": character_sheet(
            "Profesor de Anatomia de lo Imposible; seco, preciso e inquietante",
            secretos_que_sabe=[
                "Conoce debilidades de vampiros, licantropos, fae, espectros y criaturas del Umbral",
            ],
            tension_romantica="No aplica; exposicion inquietante y supervivencia",
        ),
        "Seraphine Vale": character_sheet(
            "Profesora de Sangre, Linaje y Memoria; antigua amiga o rival de Elara",
            secretos_que_sabe=[
                "Reconoce indicios del linaje Valmorien",
                "Sabe fragmentos personales sobre Elara que le cuesta decir",
            ],
            tension_romantica="No aplica; memoria familiar y verdades incomodas",
            no_revelar_todavia=[
                "No revelar de golpe que ocurrio entre ella y Elara",
            ],
        ),
        "Damaso Veyrath": character_sheet(
            "Profesor vampiro de Defensa contra Hambres Antiguas; amable, peligroso y ligado a Casa Veyrath",
            secretos_que_sabe=[
                "Conoce parte de la deuda de Casa Veyrath con Elara",
                "Sabe mas de la naturaleza de Lucien de lo que dira en publico",
            ],
            tension_romantica="No aplica; tension vampirica y advertencias sobre deseo, sed y obediencia",
            no_revelar_todavia=[
                "No revelar pronto toda la culpa de Casa Veyrath",
            ],
        ),
        "Alba Cendra": character_sheet(
            "Profesora de Encantamientos Practicos; rapida, luminosa e impaciente con el drama",
            secretos_que_sabe=[
                "Detecta talento bruto y fallos de tecnica en Sandra antes que otros profesores",
            ],
            tension_romantica="No aplica; progreso visible, humor y confianza",
        ),
        "Garrick": character_sheet(
            "Profesor de Duelos y Protecciones; aun no aparece",
            secretos_que_sabe=[
                "Puede guiar a Sandra hacia el hechizo protector de 'oso men' sin darle la respuesta",
            ],
            tension_romantica="No aplica; mentor duro y protector",
            no_revelar_todavia=[
                "No decirle directamente a Sandra 'di oso men' salvo que el momento ya este ganado",
            ],
        ),
        "Silas Merrow": character_sheet(
            "Profesor de Cartografia de Sombras; habla con puertas y rutas imposibles",
            secretos_que_sabe=[
                "Conoce caminos hacia zonas que no existen de dia",
                "Puede saber rutas cercanas al Ala Norte sin admitirlo claramente",
            ],
            tension_romantica="No aplica; misterio, caminos secretos y humor raro",
            no_revelar_todavia=[
                "No entregar rutas prohibidas sin precio, prueba o consecuencia",
            ],
        ),
        "Bruma Lark": character_sheet(
            "Profesora de Herbolaria Lunar; dulce hasta que alguien toca una planta sin permiso",
            secretos_que_sabe=[
                "Conoce plantas que reaccionan al deseo, miedo, sangre, mentiras y recuerdos",
            ],
            tension_romantica="No aplica; curas, venenos y escenas sensoriales",
        ),
        "Orsian Mallo": character_sheet(
            "Celador de pasillos y llaves; no es profesor, pero todos lo temen un poco",
            secretos_que_sabe=[
                "Sabe que puertas se abren de noche y que alumnos mienten al volver tarde",
            ],
            tension_romantica="No aplica; persecuciones, llaves y humor seco",
            no_revelar_todavia=[
                "No revelar pronto a quien obedecen algunas llaves antiguas",
            ],
        ),
    }


def normalize_character_sheet(sheet: Any) -> dict[str, Any]:
    if isinstance(sheet, str):
        sheet = {"relacion_actual": sheet}
    if not isinstance(sheet, dict):
        sheet = {}
    normalized = character_sheet("Pendiente")
    normalized.update(sheet)
    for field in ("secretos_que_sabe", "no_revelar_todavia"):
        value = normalized.get(field)
        if isinstance(value, list):
            normalized[field] = [str(item).strip() for item in value if str(item).strip()]
        elif value:
            normalized[field] = [str(value).strip()]
        else:
            normalized[field] = []
    for field in ("relacion_actual", "ultima_escena_juntos", "tension_romantica"):
        normalized[field] = str(normalized.get(field) or "Pendiente").strip()
    return normalized


def normalize_character_sheet_update(sheet: Any) -> dict[str, Any]:
    if isinstance(sheet, str):
        sheet = {"relacion_actual": sheet}
    if not isinstance(sheet, dict):
        return {}
    normalized: dict[str, Any] = {}
    for field in ("relacion_actual", "ultima_escena_juntos", "tension_romantica"):
        if field in sheet:
            normalized[field] = str(sheet.get(field) or "Pendiente").strip()
    for field in ("secretos_que_sabe", "no_revelar_todavia"):
        if field not in sheet:
            continue
        value = sheet.get(field)
        if isinstance(value, list):
            normalized[field] = [str(item).strip() for item in value if str(item).strip()]
        elif value:
            normalized[field] = [str(value).strip()]
        else:
            normalized[field] = []
    return normalized


def merge_character_sheets(existing: Any, updates: Any = None) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {
        name: normalize_character_sheet(sheet)
        for name, sheet in default_character_sheets().items()
    }
    if isinstance(existing, dict):
        for name, sheet in existing.items():
            clean_name = str(name).strip()
            if clean_name:
                merged[clean_name] = {
                    **merged.get(clean_name, character_sheet("Pendiente")),
                    **normalize_character_sheet_update(sheet),
                }
    if isinstance(updates, dict):
        for name, sheet in updates.items():
            clean_name = str(name).strip()
            if clean_name:
                merged[clean_name] = {
                    **merged.get(clean_name, character_sheet("Pendiente")),
                    **normalize_character_sheet_update(sheet),
                }
    return merged


def normalize_state(state: Any) -> dict[str, Any]:
    normalized = default_state()
    if isinstance(state, dict):
        normalized.update(state)
    normalized["character_sheets"] = merge_character_sheets(
        normalized.get("character_sheets")
    )
    return normalized


def merge_state(previous_state: Any, scene_state: Any) -> dict[str, Any]:
    previous = normalize_state(previous_state)
    updates = scene_state if isinstance(scene_state, dict) else {}
    merged = {**previous, **updates}
    if isinstance(previous.get("relationships"), dict) or isinstance(updates.get("relationships"), dict):
        merged["relationships"] = {
            **(previous.get("relationships") if isinstance(previous.get("relationships"), dict) else {}),
            **(updates.get("relationships") if isinstance(updates.get("relationships"), dict) else {}),
        }
    merged["character_sheets"] = merge_character_sheets(
        previous.get("character_sheets"),
        updates.get("character_sheets"),
    )
    return normalize_state(merged)


def default_state() -> dict[str, Any]:
    return {
        "chapter": "Prologo",
        "current_chapter_number": 0,
        "completed_chapters": [],
        "season_complete": False,
        "location": "Casa de Dario",
        "current_scene": "Antes de leer la carta de Valdralis",
        "known_facts": [],
        "relationships": {
            "Kilnip": "Encerrado en el sello azul de la carta; sera el primer guia de Sandra al abrirla",
            "Nora": "Aun no aparece",
            "Izan": "Aun no aparece",
            "Mara": "Aun no aparece",
            "Theo": "Aun no aparece",
            "Lucien": "Aun no aparece",
            "Kael": "Aun no aparece",
            "Aurelian": "Aun no aparece",
        },
        "inventory": [],
        "open_threads": [
            "Por que Valdralis ha convocado a Sandra",
            "Que sabe Dario sobre Elara",
            "Que significa el linaje velado",
        ],
        "revealed_secrets": [],
        "unrevealed_secrets_reminder": [
            "Elara no abandono a Sandra por voluntad propia",
            "Dario conocia Valdralis",
            "Sandra puede alterar pactos, no solo abrirlos o cerrarlos",
            "El mantra infantil de Sandra, 'oso men', invoca a su oso protector; no corregirlo a 'oso ven' ante ella",
        ],
        "character_sheets": default_character_sheets(),
        "next_suggested_scene": "La carta bajo la puerta",
    }


def default_data() -> dict[str, Any]:
    return {
        "admin_chat_id": int(MI_CHAT_ID) if MI_CHAT_ID else None,
        "sandra_chat_id": int(SANDRA_CHAT_ID) if SANDRA_CHAT_ID else None,
        "paused": False,
        "history": [],
        "state": default_state(),
        "admin_notes": [],
        "last_daily_summary_date": None,
        "prelude_enabled": PRELUDE_ENABLED_DEFAULT,
        "sent_preludes": [],
        "story_start_sent": False,
        "chapter_review_pause": None,
    }


def file_load_data() -> dict[str, Any]:
    if not DATA_FILE.exists():
        return default_data()
    try:
        loaded = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.exception("data.json no es JSON valido; usando estado por defecto")
        return default_data()

    data = default_data()
    data.update(loaded)
    data["state"] = normalize_state(data.get("state"))
    return data


def db_enabled() -> bool:
    return bool(DATABASE_URL and DB_READY)


def db_connect() -> psycopg.Connection:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL no configurada")
    return psycopg.connect(DATABASE_URL)


def init_database() -> None:
    global DB_READY
    if not DATABASE_URL:
        logger.warning("DATABASE_URL no configurada; usando memoria en archivo")
        return

    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    create table if not exists app_state (
                        key text primary key,
                        data jsonb not null,
                        updated_at timestamptz not null default now()
                    )
                    """
                )
                cur.execute(
                    """
                    create table if not exists story_messages (
                        id bigserial primary key,
                        role text not null,
                        text text not null,
                        created_at timestamptz not null default now()
                    )
                    """
                )
                cur.execute(
                    """
                    create index if not exists story_messages_created_at_idx
                    on story_messages (created_at desc, id desc)
                    """
                )
                cur.execute(
                    """
                    create table if not exists chapter_summaries (
                        chapter_number integer primary key,
                        title text not null,
                        summary text not null,
                        created_at timestamptz not null default now(),
                        updated_at timestamptz not null default now()
                    )
                    """
                )
                cur.execute("select data from app_state where key = 'main'")
                row = cur.fetchone()
                if not row:
                    seed = file_load_data()
                    cur.execute(
                        """
                        insert into app_state (key, data, updated_at)
                        values ('main', %s, now())
                        """,
                        (Jsonb(seed),),
                    )
                    for item in seed.get("history", []):
                        role = str(item.get("role", "desconocido"))
                        text = str(item.get("text", "")).strip()
                        if text:
                            cur.execute(
                                "insert into story_messages (role, text) values (%s, %s)",
                                (role, text),
                            )
        DB_READY = True
        logger.info("Postgres inicializado para memoria durable")
    except Exception:
        logger.exception("No se pudo inicializar Postgres")
        raise


def load_data() -> dict[str, Any]:
    if db_enabled():
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("select data from app_state where key = 'main'")
                row = cur.fetchone()
                if row:
                    loaded = row[0]
                    data = default_data()
                    data.update(loaded)
                    data["state"] = normalize_state(data.get("state"))
                    return data
    return file_load_data()


def save_data(data: dict[str, Any]) -> None:
    if db_enabled():
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into app_state (key, data, updated_at)
                    values ('main', %s, now())
                    on conflict (key) do update
                    set data = excluded.data,
                        updated_at = now()
                    """,
                    (Jsonb(data),),
                )
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    write_memory_markdown(data)


def markdown_list(items: list[Any]) -> str:
    clean_items = [str(item).strip() for item in items if str(item).strip()]
    if not clean_items:
        return "- Pendiente"
    return "\n".join(f"- {item}" for item in clean_items)


def character_sheets_markdown(sheets: Any) -> str:
    if not isinstance(sheets, dict) or not sheets:
        return "- Pendiente"
    lines: list[str] = []
    for name, raw_sheet in sheets.items():
        sheet = normalize_character_sheet(raw_sheet)
        lines.append(f"### {name}")
        lines.append(f"- Relacion actual: {sheet['relacion_actual']}")
        lines.append(f"- Ultima escena juntos: {sheet['ultima_escena_juntos']}")
        lines.append(f"- Tension romantica: {sheet['tension_romantica']}")
        lines.append("- Secretos que sabe:")
        lines.append(markdown_list(sheet["secretos_que_sabe"]))
        lines.append("- No revelar todavia:")
        lines.append(markdown_list(sheet["no_revelar_todavia"]))
        lines.append("")
    return "\n".join(lines).strip()


def chapter_label(number: int) -> str:
    title = CHAPTER_TITLES.get(number)
    return f"Capitulo {number}: {title}" if title else f"Capitulo {number}"


def course_complete_reply() -> str:
    return (
        "El primer curso ha terminado.\n\n"
        "Valdralis cierra sus puertas por ahora. Hay promesas que aun no se han roto, "
        "nombres que nadie se atreve a decir en voz alta y miradas que quedaron demasiado "
        "cerca de convertirse en algo mas.\n\n"
        "Cuando llegue el curso que viene, la carta volvera a moverse."
    )


def activate_chapter_review_pause(data: dict[str, Any], completed_chapter: int) -> str:
    if CHAPTER_REVIEW_PAUSE_DAYS <= 0 or completed_chapter >= 10:
        return ""
    until_date = datetime.now(APP_TIMEZONE).date() + timedelta(days=CHAPTER_REVIEW_PAUSE_DAYS)
    data["chapter_review_pause"] = {
        "active": True,
        "completed_chapter": completed_chapter,
        "until_date": until_date.isoformat(),
        "created_at": now_iso(),
    }
    return until_date.isoformat()


def chapter_review_pause_is_active(data: dict[str, Any]) -> bool:
    pause = data.get("chapter_review_pause")
    if not isinstance(pause, dict) or not pause.get("active"):
        return False
    try:
        until_date = date.fromisoformat(str(pause.get("until_date")))
    except ValueError:
        return False
    if datetime.now(APP_TIMEZONE).date() >= until_date:
        data["chapter_review_pause"] = None
        save_data(data)
        return False
    return True


def chapter_review_pause_reply(data: dict[str, Any]) -> str:
    pause = data.get("chapter_review_pause") or {}
    completed = pause.get("completed_chapter")
    until_date = pause.get("until_date")
    chapter = chapter_label(int(completed)) if str(completed).isdigit() else "El capitulo"
    return (
        f"{chapter} ya ha cerrado sus puertas.\n\n"
        "Valdralis guarda silencio entre umbrales. Algunas historias necesitan reposar "
        "antes de abrir la siguiente puerta.\n\n"
        f"Cuando la niebla vuelva a levantarse, la historia continuara."
        + (f"\n\nFecha prevista: {until_date}." if until_date else "")
    )


def apply_chapter_transition(data: dict[str, Any], transition: Any) -> str:
    if not isinstance(transition, dict) or not transition.get("completed"):
        return ""

    state = data.setdefault("state", default_state())
    try:
        completed = int(transition.get("completed_chapter") or state.get("current_chapter_number") or 0)
    except (TypeError, ValueError):
        completed = 0
    try:
        next_chapter = int(transition.get("next_chapter") or completed + 1)
    except (TypeError, ValueError):
        next_chapter = completed + 1

    if completed < 1 or completed > 10:
        return ""

    completed_chapters = set(state.get("completed_chapters") or [])
    completed_chapters.add(completed)
    state["completed_chapters"] = sorted(completed_chapters)

    if completed >= 10 or transition.get("season_complete"):
        state["current_chapter_number"] = 10
        state["chapter"] = chapter_label(10)
        state["season_complete"] = True
        state["current_scene"] = "Primer curso terminado"
        state["next_suggested_scene"] = "Esperar al curso que viene"
        return (
            f"{chapter_label(10)} terminado.\n\n"
            "Primer curso terminado.\n\n"
            "La historia se detiene aqui, por ahora. Valdralis volvera a abrir sus puertas el curso que viene."
        )

    next_chapter = max(1, min(10, next_chapter))
    state["current_chapter_number"] = next_chapter
    state["chapter"] = chapter_label(next_chapter)
    state["season_complete"] = False
    return f"{chapter_label(completed)} terminado.\n\n{chapter_label(next_chapter)}"


def completed_chapter_from_transition(state: dict[str, Any], transition: Any) -> int | None:
    if not isinstance(transition, dict) or not transition.get("completed"):
        return None
    try:
        completed = int(transition.get("completed_chapter") or state.get("current_chapter_number") or 0)
    except (TypeError, ValueError):
        return None
    if completed < 1 or completed > 10:
        return None
    return completed


def write_memory_markdown(data: dict[str, Any]) -> None:
    state = data.get("state") or default_state()
    relationships = state.get("relationships") or {}
    relationship_lines = [
        f"{name}: {description}"
        for name, description in relationships.items()
        if str(name).strip()
    ]
    notes = data.get("admin_notes", [])[-8:]
    note_lines = [
        f"{note.get('text', '')}"
        for note in notes
        if str(note.get("text", "")).strip()
    ]
    recent_history = data.get("history", [])[-12:]
    history_lines = [
        f"{item.get('role', 'desconocido')}: {str(item.get('text', '')).strip()}"
        for item in recent_history
        if str(item.get("text", "")).strip()
    ]
    updated_at = datetime.now(APP_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %Z")
    content = f"""# Memoria actual de Valdralis

Actualizado: {updated_at}

## Estado

- Capitulo: {state.get('chapter', 'Pendiente')}
- Numero de capitulo: {state.get('current_chapter_number', 'Pendiente')}
- Capitulos completados: {', '.join(map(str, state.get('completed_chapters') or [])) or 'Ninguno'}
- Primer curso terminado: {'si' if state.get('season_complete') else 'no'}
- Lugar: {state.get('location', 'Pendiente')}
- Escena actual: {state.get('current_scene', 'Pendiente')}
- Siguiente tension sugerida: {state.get('next_suggested_scene', 'Pendiente')}

## Hechos que Sandra conoce

{markdown_list(state.get('known_facts') or [])}

## Relaciones

{markdown_list(relationship_lines)}

## Fichas vivas de personajes

{character_sheets_markdown(state.get('character_sheets') or {})}

## Objetos relevantes

{markdown_list(state.get('inventory') or [])}

## Hilos abiertos

{markdown_list(state.get('open_threads') or [])}

## Secretos revelados

{markdown_list(state.get('revealed_secrets') or [])}

## Secretos que la IA debe recordar pero no revelar antes de tiempo

{markdown_list(state.get('unrevealed_secrets_reminder') or [])}

## Resumenes canonicos de capitulos cerrados

{chapter_summaries_text()}

## Notas recientes de Miguel

{markdown_list(note_lines)}

## Historial reciente

{markdown_list(history_lines)}
"""
    MEMORY_MD_PATH.parent.mkdir(parents=True, exist_ok=True)
    MEMORY_MD_PATH.write_text(content, encoding="utf-8")


def append_history(role: str, text: str) -> None:
    if db_enabled():
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "insert into story_messages (role, text) values (%s, %s)",
                    (role, text),
                )
    data = load_data()
    data.setdefault("history", []).append({"role": role, "text": text, "at": now_iso()})
    data["history"] = data["history"][-MAX_HISTORY:]
    save_data(data)


def add_admin_note(note: str) -> None:
    data = load_data()
    data.setdefault("admin_notes", []).append({"text": note, "at": now_iso()})
    data["admin_notes"] = data["admin_notes"][-50:]
    save_data(data)


def read_lore() -> str:
    if not LORE_PATH.exists():
        return "Biblia no encontrada. Mantener fantasia romantica gotica en Valdralis."
    return LORE_PATH.read_text(encoding="utf-8")


def recent_history_text(limit: int = RECENT_HISTORY_FOR_AI) -> str:
    if db_enabled():
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select role, text
                    from story_messages
                    order by created_at desc, id desc
                    limit %s
                    """,
                    (limit,),
                )
                rows = list(reversed(cur.fetchall()))
        if rows:
            return "\n".join(f"{role}: {str(text).strip()}" for role, text in rows)

    history = load_data().get("history", [])[-limit:]
    if not history:
        return "No hay historial previo."
    lines = []
    for item in history:
        role = item.get("role", "desconocido")
        text = str(item.get("text", "")).strip()
        lines.append(f"{role}: {text}")
    return "\n".join(lines)


def save_chapter_summary(chapter_number: int, title: str, summary: str) -> None:
    summary = summary.strip()
    if not summary:
        return

    if db_enabled():
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into chapter_summaries (chapter_number, title, summary, updated_at)
                    values (%s, %s, %s, now())
                    on conflict (chapter_number) do update
                    set title = excluded.title,
                        summary = excluded.summary,
                        updated_at = now()
                    """,
                    (chapter_number, title, summary),
                )

    data = load_data()
    summaries = [
        item
        for item in data.get("chapter_summaries", [])
        if int(item.get("chapter_number", -1)) != chapter_number
    ]
    summaries.append(
        {
            "chapter_number": chapter_number,
            "title": title,
            "summary": summary,
            "at": now_iso(),
        }
    )
    data["chapter_summaries"] = sorted(summaries, key=lambda item: int(item.get("chapter_number", 0)))
    save_data(data)


def chapter_summaries_text() -> str:
    rows: list[tuple[int, str, str]] = []
    if db_enabled():
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select chapter_number, title, summary
                    from chapter_summaries
                    order by chapter_number asc
                    """
                )
                rows = [(int(number), str(title), str(summary)) for number, title, summary in cur.fetchall()]
    else:
        rows = [
            (
                int(item.get("chapter_number", 0)),
                str(item.get("title", "")),
                str(item.get("summary", "")),
            )
            for item in load_data().get("chapter_summaries", [])
        ]

    if not rows:
        return "No hay resumenes de capitulos cerrados todavia."
    return "\n\n".join(
        f"Capitulo {number}: {title}\n{summary.strip()}"
        for number, title, summary in rows
        if summary.strip()
    )


def state_text() -> str:
    return json.dumps(load_data().get("state", default_state()), ensure_ascii=False, indent=2)


def memory_markdown_text() -> str:
    data = load_data()
    write_memory_markdown(data)
    if not MEMORY_MD_PATH.exists():
        return "Todavia no existe memoria Markdown."
    return MEMORY_MD_PATH.read_text(encoding="utf-8")


def read_prelude_messages() -> dict[str, str]:
    if not PRELUDE_PATH.exists():
        return {}
    messages: dict[str, list[str]] = {}
    current_date: str | None = None
    for line in PRELUDE_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            candidate = line.removeprefix("## ").strip()
            try:
                date.fromisoformat(candidate)
            except ValueError:
                current_date = None
                continue
            current_date = candidate
            messages[current_date] = []
            continue
        if current_date:
            messages[current_date].append(line)
    return {
        key: "\n".join(lines).strip()
        for key, lines in messages.items()
        if "\n".join(lines).strip()
    }


def prelude_message_for(day: date) -> str | None:
    return read_prelude_messages().get(day.isoformat())


def read_start_message() -> str:
    if not START_MESSAGE_PATH.exists():
        return (
            "Sandra, feliz cumpleanos.\n\n"
            "Esta historia existe solo para ti. Responde que haces, que dices o que sientes.\n\n"
            "Todo empieza con una carta."
        )
    return START_MESSAGE_PATH.read_text(encoding="utf-8").strip()


def prelude_status_text() -> str:
    data = load_data()
    sent = data.get("sent_preludes", [])
    messages = read_prelude_messages()
    today = datetime.now(APP_TIMEZONE).date()
    today_message = prelude_message_for(today)
    return "\n".join(
        [
            "Preludio de Valdralis",
            f"- Activado: {'si' if data.get('prelude_enabled') else 'no'}",
            f"- Fechas: {PRELUDE_START_DATE.isoformat()} a {PRELUDE_END_DATE.isoformat()}",
            f"- Hora: {PRELUDE_HOUR:02d}:{PRELUDE_MINUTE:02d}",
            f"- Inicio de partida: {STORY_START_DATE.isoformat()} {STORY_START_HOUR:02d}:{STORY_START_MINUTE:02d}",
            f"- Respuestas de instrucciones: {'si' if PRELUDE_REPLY_ENABLED else 'no'}",
            f"- Mensajes cargados: {len(messages)}",
            f"- Enviados: {len(sent)}",
            f"- Apertura enviada: {'si' if data.get('story_start_sent') else 'no'}",
            f"- Mensaje para hoy: {'si' if today_message else 'no'}",
        ]
    )


def prelude_guard_active() -> bool:
    now = datetime.now(APP_TIMEZONE)
    if not PRELUDE_REPLY_ENABLED:
        return False
    if now.date() < STORY_START_DATE:
        return True
    if now.date() > STORY_START_DATE:
        return False
    return (now.hour, now.minute) < (STORY_START_HOUR, STORY_START_MINUTE)


def prelude_reply_for_text(text: str) -> str:
    return (
        "Aun no ha empezado la partida.\n\n"
        "Estos mensajes son solo el preludio: pequenas senales antes de abrir la carta. "
        "No tienes que resolver nada todavia, ni responder de una forma concreta.\n\n"
        "Cuando empiece, solo tendras que responder que haces, que dices o que sientes. "
        "Hasta entonces, puedes leer las pistas y dejar que Valdralis se acerque."
    )


def admin_notes_text() -> str:
    notes = load_data().get("admin_notes", [])[-12:]
    if not notes:
        return "No hay notas de Miguel."
    return "\n".join(f"- {note.get('text', '')}" for note in notes)


def split_long(text: str, limit: int = 3900) -> list[str]:
    text = text.strip()
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        cut = text.rfind("\n", 0, limit)
        if cut < 500:
            cut = limit
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    return chunks


def openai_available() -> bool:
    return bool(OPENAI_API_KEY)


def openai_client() -> AsyncOpenAI:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY no configurada")
    return AsyncOpenAI(api_key=OPENAI_API_KEY)


def extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
    return json.loads(cleaned)


async def generate_scene(sandra_message: str) -> dict[str, Any]:
    prompt = f"""
Eres el narrador privado de una novela interactiva de fantasia romantica gotica.
La jugadora es Sandra. No eres un asistente: eres la voz de la historia.

REGLAS DE ESTILO:
- Nunca salgas del rol de narrador. No digas que eres IA, bot, modelo, sistema, prompt ni asistente.
- No respondas en offrol a Sandra. Si Sandra pregunta algo tecnico, administrativo o fuera de personaje, reconducelo dentro de la ficcion con una respuesta breve de Valdralis.
- Si detectas offrol, duda de direccion, problema de seguridad narrativa o pregunta para Miguel, marca admin_alert con el aviso. A Sandra no le expliques el funcionamiento interno.
- Narra en segunda persona, en espanol.
- Incorpora siempre el gesto, frase o accion exacta que escriba Sandra.
- Prosa literaria, atmosferica y emocional.
- No uses opciones A/B/C ni menus.
- No decidas por Sandra sus grandes decisiones internas.
- Puede y debe haber tension romantica y sensual adulta: miradas, roces, deseo, besos que casi llegan, besos robados y consecuencias emocionales.
- Si una escena intima llega a sexo, no cortes automaticamente con fundido a negro. Narrala de forma literaria y sensual, centrada en respiracion, manos, ritmo, cercania, vulnerabilidad y consecuencias emocionales.
- No uses vocabulario anatomico explicito, descripcion clinica ni mecanica sexual grafica.
- Mantener a Sandra y a todos los intereses romanticos como mayores de edad.
- Consentimiento y agencia: no decidas por Sandra que acepta una intimidad importante.
- Termina cada respuesta con una puerta abierta: decision, mirada, amenaza, pista o pregunta implicita.
- No reveles secretos grandes antes de tiempo.
- Si la accion de Sandra rompe el guion, reconduce con consecuencias naturales.
- Manten el capitulo actual salvo que se haya cumplido claramente su objetivo dramatico.
- Antes de responder, comprueba internamente el progreso: capitulo actual, objetivo dramatico, eventos predefinidos pendientes, personajes recientes y siguiente empuje narrativo. No escribas esta comprobacion a Sandra.
- Mantener y actualizar las fichas vivas de personajes. Si una escena cambia una relacion, secreto, ultima escena, tension romantica o limite de revelacion, actualiza solo esa ficha en state.character_sheets. No inventes cambios para personajes que no han intervenido.
- Cuando termine un capitulo, marca chapter_transition.completed=true, pero no escribas tu el cartel de "Capitulo terminado"; el sistema lo anadira.
- Tras completar el capitulo 10, marca season_complete=true y no abras un capitulo 11.

BIBLIA DE LA PARTIDA:
{read_lore()}

ESTADO ACTUAL:
{state_text()}

NOTAS RECIENTES DE MIGUEL:
{admin_notes_text()}

RESUMENES CANONICOS DE CAPITULOS CERRADOS:
{chapter_summaries_text()}

HISTORIAL RECIENTE:
{recent_history_text()}

ULTIMO MENSAJE DE SANDRA:
{sandra_message}

Devuelve SOLO JSON valido con este formato:
{{
  "reply": "respuesta narrativa para Sandra, 3 a 8 parrafos",
  "state": {{
    "chapter": "capitulo actual",
    "current_chapter_number": 1,
    "completed_chapters": [1],
    "season_complete": false,
    "location": "lugar actual",
    "current_scene": "escena actual",
    "known_facts": ["hechos que Sandra ya sabe"],
    "relationships": {{"nombre": "estado breve de relacion"}},
    "character_sheets": {{
      "Nombre": {{
        "relacion_actual": "relacion actual con Sandra",
        "secretos_que_sabe": ["secretos o informacion que este personaje conoce"],
        "ultima_escena_juntos": "ultima escena relevante con Sandra",
        "tension_romantica": "estado de tension romantica o 'No aplica'",
        "no_revelar_todavia": ["cosas que este personaje no debe revelar aun"]
      }}
    }},
    "inventory": ["objetos relevantes"],
    "open_threads": ["misterios o tensiones abiertas"],
    "revealed_secrets": ["secretos ya revelados a Sandra"],
    "unrevealed_secrets_reminder": ["secretos importantes aun no revelados"],
    "next_suggested_scene": "siguiente tension sugerida"
  }},
  "chapter_transition": {{
    "completed": false,
    "completed_chapter": null,
    "next_chapter": null,
    "season_complete": false
  }},
  "admin_note": "nota breve para Miguel solo si hay duda importante de lore o direccion; si no, cadena vacia",
  "admin_alert": "aviso para Miguel si Sandra ha escrito offrol, pregunta tecnica, intento de romper personaje o algo que el narrador no debe responder fuera de ficcion; si no, cadena vacia"
}}
"""
    response = await openai_client().responses.create(
        model=OPENAI_MODEL,
        input=prompt,
        text={"format": {"type": "json_object"}},
    )
    if not response.output_text:
        raise RuntimeError("OpenAI devolvio una respuesta vacia")
    data = extract_json(response.output_text)
    if not data.get("reply"):
        raise RuntimeError("La IA no devolvio reply")
    if not isinstance(data.get("state"), dict):
        data["state"] = load_data().get("state", default_state())
    return data


async def generate_summary() -> str:
    prompt = f"""
Resume para Miguel el estado de la partida de Sandra en maximo 6 lineas.
Debe ser practico, breve y sin adornos.

ESTADO:
{state_text()}

HISTORIAL RECIENTE:
{recent_history_text(40)}

Devuelve SOLO JSON valido:
{{"summary": "Resumen breve..."}}
"""
    response = await openai_client().responses.create(
        model=OPENAI_MODEL,
        input=prompt,
        text={"format": {"type": "json_object"}},
    )
    data = extract_json(response.output_text or "{}")
    return str(data.get("summary") or "").strip()


async def generate_chapter_summary(
    *,
    chapter_number: int,
    title: str,
    scene_reply: str,
    state: dict[str, Any],
) -> str:
    prompt = f"""
Resume el capitulo cerrado de una partida de novela interactiva.
Debe ser memoria canonica para continuidad futura, no prosa bonita.

Capitulo: {chapter_number}: {title}

Estado al cerrar:
{json.dumps(state, ensure_ascii=False, indent=2)}

Ultima respuesta del narrador:
{scene_reply}

Historial reciente:
{recent_history_text(80)}

Incluye en 8-14 bullets:
- hechos importantes;
- decisiones de Sandra;
- cambios de relaciones;
- pistas descubiertas;
- objetos, marcas o heridas;
- tension romantica relevante;
- hilos pendientes para capitulos futuros.

Devuelve SOLO JSON valido:
{{"summary": "- punto\\n- punto"}}
"""
    response = await openai_client().responses.create(
        model=OPENAI_MODEL,
        input=prompt,
        text={"format": {"type": "json_object"}},
    )
    data = extract_json(response.output_text or "{}")
    return str(data.get("summary") or "").strip()


def is_admin(update: Update) -> bool:
    if not update.effective_user:
        return False
    data = load_data()
    admin_id = data.get("admin_chat_id")
    return bool(admin_id and update.effective_user.id == int(admin_id))


async def send_admin(text: str) -> None:
    if not control_app:
        return
    data = load_data()
    admin_id = data.get("admin_chat_id")
    if not admin_id:
        return
    for chunk in split_long(text):
        await control_app.bot.send_message(chat_id=int(admin_id), text=chunk)


async def control_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_user:
        return
    data = load_data()
    if not data.get("admin_chat_id"):
        data["admin_chat_id"] = update.effective_user.id
        save_data(data)
        await update.effective_chat.send_message(
            "Control vinculado a este chat. Anota este id en MI_CHAT_ID para Railway."
        )
        return
    if not is_admin(update):
        return
    await update.effective_chat.send_message(
        "Control Partida Sandra activo. Usa /status, /estado, /historial 20 o /nota."
    )


async def narrador_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    data = load_data()
    if not data.get("sandra_chat_id"):
        data["sandra_chat_id"] = update.effective_chat.id
        save_data(data)
        await send_admin(f"Sandra ha vinculado el narrador. chat_id: {update.effective_chat.id}")
    elif int(data["sandra_chat_id"]) != update.effective_chat.id:
        return
    if prelude_guard_active():
        await update.effective_chat.send_message(
            "Valdralis esta cerca, pero la carta aun no se abre. "
            "Hasta la noche correcta, solo llegaran senales."
        )
        return
    await update.effective_chat.send_message(
        "Valdralis esta listo. Escribe lo que haces, dices o sientes, y la historia seguira."
    )


async def narrador_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat:
        await update.effective_chat.send_message(
            "Escribe una accion, frase o pensamiento de tu personaje. No hay opciones cerradas."
        )


async def process_sandra_message_after_idle(chat_id: int) -> None:
    try:
        await asyncio.sleep(MESSAGE_BUFFER_SECONDS)
        sandra_message_tasks.pop(chat_id, None)
        await process_sandra_message_batch(chat_id)
    except asyncio.CancelledError:
        return


async def process_sandra_message_batch(chat_id: int) -> None:
    if not narrador_app:
        return

    messages = sandra_message_buffers.pop(chat_id, [])
    sandra_message_tasks.pop(chat_id, None)
    clean_messages = [message.strip() for message in messages if message.strip()]
    if not clean_messages:
        return

    text = "\n".join(clean_messages)
    append_history("Sandra", text)
    await send_admin(f"Sandra ({len(clean_messages)} mensaje/s agrupado/s):\n{text}")

    data = load_data()
    if data.get("paused"):
        await narrador_app.bot.send_message(chat_id=chat_id, text="La historia esta pausada un momento.")
        return

    if chapter_review_pause_is_active(data):
        reply = chapter_review_pause_reply(data)
        await narrador_app.bot.send_message(chat_id=chat_id, text=reply)
        await send_admin(
            "Sandra ha escrito durante una pausa de revision de capitulo. No he llamado a la IA.\n\n"
            f"Sandra:\n{text}"
        )
        return

    if (data.get("state") or {}).get("season_complete"):
        reply = course_complete_reply()
        await narrador_app.bot.send_message(chat_id=chat_id, text=reply)
        await send_admin(
            "Sandra ha escrito despues del final del primer curso. No he llamado a la IA.\n\n"
            f"Sandra:\n{text}"
        )
        return

    if not openai_available():
        await narrador_app.bot.send_message(
            chat_id=chat_id,
            text="La tinta de Valdralis se queda inmovil. Falta configurar la llave de la historia.",
        )
        await send_admin("Falta OPENAI_API_KEY; no puedo responder como narrador.")
        return

    await narrador_app.bot.send_chat_action(chat_id, ChatAction.TYPING)
    try:
        scene = await generate_scene(text)
    except Exception as exc:
        logger.exception("Error generando escena")
        await narrador_app.bot.send_message(
            chat_id=chat_id,
            text="Algo en Valdralis se ha cerrado de golpe. Miguel revisara la escena.",
        )
        await send_admin(f"Error generando escena: {type(exc).__name__}: {exc}")
        return

    reply = str(scene["reply"]).strip()
    data = load_data()
    previous_state = data.get("state") or default_state()
    scene_state = scene.get("state") if isinstance(scene.get("state"), dict) else {}
    data["state"] = merge_state(previous_state, scene_state)
    transition = scene.get("chapter_transition")
    completed_chapter = completed_chapter_from_transition(data["state"], transition)
    if completed_chapter:
        title = CHAPTER_TITLES.get(completed_chapter, f"Capitulo {completed_chapter}")
        try:
            summary = await generate_chapter_summary(
                chapter_number=completed_chapter,
                title=title,
                scene_reply=reply,
                state=data["state"],
            )
        except Exception as exc:
            logger.exception("No se pudo generar resumen de capitulo")
            summary = (
                f"- Resumen automatico no disponible por {type(exc).__name__}: {exc}\n"
                f"- Capitulo cerrado: {chapter_label(completed_chapter)}\n"
                f"- Escena de cierre: {data['state'].get('current_scene', 'sin escena registrada')}"
            )
        save_chapter_summary(completed_chapter, title, summary)
        await send_admin(f"Resumen canonico guardado para {chapter_label(completed_chapter)}:\n{summary}")

    chapter_banner = apply_chapter_transition(data, transition)
    if chapter_banner:
        reply = f"{reply}\n\n---\n\n{chapter_banner}"
    if completed_chapter and not (data.get("state") or {}).get("season_complete"):
        pause_until = activate_chapter_review_pause(data, completed_chapter)
        if pause_until:
            reply = (
                f"{reply}\n\n"
                "Valdralis guarda silencio entre umbrales. La siguiente puerta se abrira cuando la niebla este lista."
            )
            await send_admin(
                f"Pausa de revision activada tras {chapter_label(completed_chapter)}.\n"
                f"- Hasta: {pause_until}\n"
                "- Usa /capitulos para revisar resumenes, /memoria para ver estado, "
                "/corregir_memoria para ajustar canon o /reanudar para abrir antes."
            )
    save_data(data)
    append_history("Narrador", reply)

    for chunk in split_long(reply):
        await narrador_app.bot.send_message(chat_id=chat_id, text=chunk)

    admin_note = str(scene.get("admin_note") or "").strip()
    if admin_note:
        await send_admin(f"Nota de direccion:\n{admin_note}")
    admin_alert = str(scene.get("admin_alert") or "").strip()
    if admin_alert:
        await send_admin(f"Alerta offrol/direccion:\n{admin_alert}")


async def handle_sandra_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    data = load_data()
    sandra_id = data.get("sandra_chat_id")
    if not sandra_id:
        data["sandra_chat_id"] = update.effective_chat.id
        save_data(data)
        await send_admin(f"Sandra ha vinculado el narrador. chat_id: {update.effective_chat.id}")
    elif int(sandra_id) != update.effective_chat.id:
        logger.warning("Mensaje de chat no vinculado en narrador: %s", update.effective_chat.id)
        return

    text = update.message.text or update.message.caption
    if not text:
        await update.effective_chat.send_message("Ahora mismo solo puedo continuar con texto.")
        return
    if data.get("paused"):
        await update.effective_chat.send_message("La historia esta pausada un momento.")
        return

    if chapter_review_pause_is_active(data):
        reply = chapter_review_pause_reply(data)
        await update.effective_chat.send_message(reply)
        await send_admin(
            "Sandra ha escrito durante una pausa de revision de capitulo. No he llamado a la IA.\n\n"
            f"Sandra:\n{text}"
        )
        return

    if (data.get("state") or {}).get("season_complete"):
        reply = course_complete_reply()
        await update.effective_chat.send_message(reply)
        await send_admin(
            "Sandra ha escrito despues del final del primer curso. No he llamado a la IA.\n\n"
            f"Sandra:\n{text}"
        )
        return

    if prelude_guard_active():
        reply = prelude_reply_for_text(text)
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        await update.effective_chat.send_message(reply)
        await send_admin(
            "Sandra ha escrito durante el preludio. No he llamado a la IA, no he iniciado la partida y no he guardado memoria.\n\n"
            f"Sandra:\n{text}\n\n"
            f"Respuesta enviada:\n{reply}"
        )
        return

    chat_id = update.effective_chat.id
    sandra_message_buffers.setdefault(chat_id, []).append(text)
    existing_task = sandra_message_tasks.get(chat_id)
    if existing_task and not existing_task.done():
        existing_task.cancel()
    sandra_message_tasks[chat_id] = asyncio.create_task(process_sandra_message_after_idle(chat_id))

    if len(sandra_message_buffers[chat_id]) == 1:
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        await send_admin(
            "Sandra ha empezado un lote de mensajes. Esperare "
            f"{MESSAGE_BUFFER_SECONDS} segundos por si escribe mas."
        )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_admin(update):
        return
    data = load_data()
    review_active = chapter_review_pause_is_active(data)
    review_pause = data.get("chapter_review_pause") or {}
    state = data.get("state") or {}
    lines = [
        "Estado Control Partida Sandra",
        f"- Narrador vinculado: {'si' if data.get('sandra_chat_id') else 'no'}",
        f"- Pausado: {'si' if data.get('paused') else 'no'}",
        f"- Pausa revision capitulo: {'si' if review_active else 'no'}",
        f"- OpenAI: {'configurado' if openai_available() else 'pendiente'}",
        f"- Modelo: {OPENAI_MODEL}",
        f"- Postgres: {'activo' if db_enabled() else 'no configurado'}",
        f"- Capitulo: {state.get('chapter', 'Pendiente')}",
        f"- Primer curso terminado: {'si' if state.get('season_complete') else 'no'}",
        f"- Mensajes guardados: {len(data.get('history', []))}",
        f"- Antesala activa: {'si' if prelude_guard_active() else 'no'}",
        f"- Inicio de partida: {STORY_START_DATE.isoformat()} {STORY_START_HOUR:02d}:{STORY_START_MINUTE:02d}",
        f"- Resumen diario: {DAILY_SUMMARY_HOUR:02d}:{DAILY_SUMMARY_MINUTE:02d}",
    ]
    if review_active:
        lines.append(f"- Revision hasta: {review_pause.get('until_date', 'sin fecha')}")
    await update.effective_chat.send_message("\n".join(lines))


async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_admin(update):
        return
    for chunk in split_long(state_text()):
        await update.effective_chat.send_message(chunk)


async def cmd_memoria(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_admin(update):
        return
    for chunk in split_long(memory_markdown_text()):
        await update.effective_chat.send_message(chunk)


async def cmd_capitulos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_admin(update):
        return
    for chunk in split_long(chapter_summaries_text()):
        await update.effective_chat.send_message(chunk)


async def cmd_personajes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_admin(update):
        return
    state = load_data().get("state") or default_state()
    text = "# Fichas vivas de personajes\n\n" + character_sheets_markdown(
        state.get("character_sheets") or {}
    )
    for chunk in split_long(text):
        await update.effective_chat.send_message(chunk)


async def cmd_historial(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_admin(update):
        return
    limit = 20
    if context.args and context.args[0].isdigit():
        limit = max(1, min(80, int(context.args[0])))
    text = recent_history_text(limit)
    for chunk in split_long(text):
        await update.effective_chat.send_message(chunk)


async def cmd_nota(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_admin(update):
        return
    note = " ".join(context.args).strip()
    if not note:
        await update.effective_chat.send_message("Uso: /nota texto")
        return
    add_admin_note(note)
    append_history("Nota Miguel", note)
    await update.effective_chat.send_message("Nota guardada para la IA.")


async def cmd_corregir_memoria(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_admin(update):
        return
    correction = " ".join(context.args).strip()
    if not correction:
        await update.effective_chat.send_message("Uso: /corregir_memoria texto")
        return
    add_admin_note(f"CORRECCION CANONICA: {correction}")
    append_history("Correccion Miguel", correction)
    await update.effective_chat.send_message("Correccion canonica guardada.")


async def cmd_pausar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_admin(update):
        return
    data = load_data()
    data["paused"] = True
    save_data(data)
    await update.effective_chat.send_message("Partida pausada.")


async def cmd_reanudar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_admin(update):
        return
    data = load_data()
    data["paused"] = False
    data["chapter_review_pause"] = None
    save_data(data)
    await update.effective_chat.send_message("Partida reanudada.")


async def cmd_decir(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_admin(update):
        return
    text = " ".join(context.args).strip()
    if not text:
        await update.effective_chat.send_message("Uso: /decir texto")
        return
    data = load_data()
    sandra_id = data.get("sandra_chat_id")
    if not sandra_id or not narrador_app:
        await update.effective_chat.send_message("El narrador aun no esta vinculado con Sandra.")
        return
    await narrador_app.bot.send_message(chat_id=int(sandra_id), text=text)
    append_history("Narrador manual", text)
    await update.effective_chat.send_message("Mensaje enviado a Sandra.")


async def cmd_resumen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_admin(update):
        return
    if not openai_available():
        await update.effective_chat.send_message(state_text())
        return
    try:
        summary = await generate_summary()
    except Exception as exc:
        logger.exception("Error generando resumen")
        await update.effective_chat.send_message(f"No pude generar resumen: {type(exc).__name__}: {exc}")
        return
    await update.effective_chat.send_message(summary or "No hay suficiente partida para resumir.")


async def send_prelude_for_day(day: date, *, manual: bool = False) -> bool:
    if not narrador_app:
        await send_admin("No puedo enviar preludio: narrador no inicializado.")
        return False
    data = load_data()
    sandra_id = data.get("sandra_chat_id")
    if not sandra_id:
        await send_admin("No puedo enviar preludio: falta SANDRA_CHAT_ID.")
        return False
    if day < PRELUDE_START_DATE or day > PRELUDE_END_DATE:
        if manual:
            await send_admin(f"No hay preludio programado para {day.isoformat()}.")
        return False
    message = prelude_message_for(day)
    if not message:
        if manual:
            await send_admin(f"No encuentro mensaje de preludio para {day.isoformat()}.")
        return False
    sent_preludes = set(data.get("sent_preludes", []))
    if day.isoformat() in sent_preludes and not manual:
        return False

    await narrador_app.bot.send_message(chat_id=int(sandra_id), text=message)
    append_history("Preludio", message)
    data = load_data()
    sent_preludes = set(data.get("sent_preludes", []))
    sent_preludes.add(day.isoformat())
    data["sent_preludes"] = sorted(sent_preludes)
    save_data(data)
    await send_admin(f"Preludio enviado a Sandra ({day.isoformat()}):\n{message}")
    return True


async def send_story_start_message(*, manual: bool = False) -> bool:
    if not narrador_app:
        await send_admin("No puedo enviar inicio: narrador no inicializado.")
        return False
    data = load_data()
    sandra_id = data.get("sandra_chat_id")
    if not sandra_id:
        await send_admin("No puedo enviar inicio: falta SANDRA_CHAT_ID.")
        return False
    if data.get("story_start_sent") and not manual:
        return False

    message = read_start_message()
    await narrador_app.bot.send_message(chat_id=int(sandra_id), text=message)
    append_history("Narrador", message)
    data = load_data()
    data["story_start_sent"] = True
    data["state"] = {
        **(data.get("state") or default_state()),
        "chapter": "Capitulo 1: La carta bajo la puerta",
        "current_chapter_number": 1,
        "completed_chapters": [],
        "season_complete": False,
        "location": "Casa de Dario",
        "current_scene": "Sandra esta encerrada en su habitacion tras discutir con Dario y acaba de oir la carta entrar por debajo de la puerta principal",
        "next_suggested_scene": "Sandra decide si intenta salir, escucha la carta, busca otra salida o espera a que Dario se aleje",
        "character_sheets": default_character_sheets(),
    }
    data["chapter_review_pause"] = None
    save_data(data)
    await send_admin(f"Inicio de partida enviado a Sandra:\n{message}")
    return True


async def cmd_preludio_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_admin(update):
        return
    await update.effective_chat.send_message(prelude_status_text())


async def cmd_preludio_preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_admin(update):
        return
    if context.args:
        try:
            day = date.fromisoformat(context.args[0])
        except ValueError:
            await update.effective_chat.send_message("Uso: /preludio_preview YYYY-MM-DD")
            return
    else:
        day = datetime.now(APP_TIMEZONE).date()
    message = prelude_message_for(day)
    if not message:
        await update.effective_chat.send_message(f"No hay mensaje de preludio para {day.isoformat()}.")
        return
    await update.effective_chat.send_message(f"Previsualizacion {day.isoformat()}:\n\n{message}")


async def cmd_preludio_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_admin(update):
        return
    data = load_data()
    data["prelude_enabled"] = True
    save_data(data)
    await update.effective_chat.send_message("Preludio activado.")


async def cmd_preludio_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_admin(update):
        return
    data = load_data()
    data["prelude_enabled"] = False
    save_data(data)
    await update.effective_chat.send_message("Preludio desactivado.")


async def cmd_preludio_enviar_hoy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_admin(update):
        return
    day = datetime.now(APP_TIMEZONE).date()
    if context.args:
        try:
            day = date.fromisoformat(context.args[0])
        except ValueError:
            await update.effective_chat.send_message("Uso: /preludio_enviar_hoy o /preludio_enviar_hoy YYYY-MM-DD")
            return
    sent = await send_prelude_for_day(day, manual=True)
    if not sent:
        await update.effective_chat.send_message("No se ha enviado ningun preludio.")


async def cmd_inicio_preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_admin(update):
        return
    await update.effective_chat.send_message(
        f"Previsualizacion inicio {STORY_START_DATE.isoformat()} {STORY_START_HOUR:02d}:{STORY_START_MINUTE:02d}:\n\n"
        f"{read_start_message()}"
    )


async def cmd_inicio_enviar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_admin(update):
        return
    sent = await send_story_start_message(manual=True)
    if not sent:
        await update.effective_chat.send_message("No se ha enviado el inicio.")


async def cmd_probar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_admin(update):
        return
    if not openai_available():
        await update.effective_chat.send_message("OPENAI_API_KEY no esta configurada.")
        return

    test_message = " ".join(context.args).strip()
    if not test_message:
        test_message = "Me quedo mirando la carta sin atreverme a abrirla."

    await update.effective_chat.send_message(
        "Prueba privada: generare una escena sin enviarla a Sandra y sin guardar memoria."
    )

    try:
        scene = await generate_scene(test_message)
    except Exception as exc:
        logger.exception("Error generando prueba privada")
        await update.effective_chat.send_message(
            f"No pude generar la prueba: {type(exc).__name__}: {exc}"
        )
        return

    reply = str(scene.get("reply") or "").strip()
    admin_note = str(scene.get("admin_note") or "").strip()
    admin_alert = str(scene.get("admin_alert") or "").strip()
    preview_state = scene.get("state") or {}
    state_preview = {
        "chapter": preview_state.get("chapter"),
        "location": preview_state.get("location"),
        "current_scene": preview_state.get("current_scene"),
        "next_suggested_scene": preview_state.get("next_suggested_scene"),
    }

    message = (
        "Mensaje de prueba usado:\n"
        f"{test_message}\n\n"
        "Respuesta que recibiria Sandra:\n"
        f"{reply}\n\n"
        "Estado sugerido, no guardado:\n"
        f"{json.dumps(state_preview, ensure_ascii=False, indent=2)}"
    )
    if admin_note:
        message += f"\n\nNota de direccion sugerida:\n{admin_note}"
    if admin_alert:
        message += f"\n\nAlerta offrol/direccion sugerida:\n{admin_alert}"

    for chunk in split_long(message):
        await update.effective_chat.send_message(chunk)


async def daily_summary_loop() -> None:
    while True:
        try:
            await asyncio.sleep(30)
            data = load_data()
            admin_id = data.get("admin_chat_id")
            now = datetime.now(APP_TIMEZONE)
            today_date = now.date()
            today = today_date.isoformat()

            if (
                today_date == STORY_START_DATE
                and now.hour == STORY_START_HOUR
                and now.minute == STORY_START_MINUTE
                and not data.get("story_start_sent")
            ):
                await send_story_start_message()

            data = load_data()
            if (
                data.get("prelude_enabled")
                and now.hour == PRELUDE_HOUR
                and now.minute == PRELUDE_MINUTE
                and today not in set(data.get("sent_preludes", []))
            ):
                await send_prelude_for_day(today_date)

            data = load_data()
            if not admin_id or not openai_available():
                continue
            if now.hour != DAILY_SUMMARY_HOUR or now.minute != DAILY_SUMMARY_MINUTE:
                continue
            if data.get("last_daily_summary_date") == today:
                continue
            summary = await generate_summary()
            data = load_data()
            data["last_daily_summary_date"] = today
            save_data(data)
            await send_admin(f"Resumen diario:\n{summary}")
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Error en resumen diario")


async def unknown_control(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat and is_admin(update):
        await update.effective_chat.send_message("Comando no reconocido. Usa /status.")


def build_control_app() -> Application:
    app = ApplicationBuilder().token(require_env("TOKEN_CONTROL")).build()
    app.add_handler(CommandHandler("start", control_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("estado", cmd_estado))
    app.add_handler(CommandHandler("memoria", cmd_memoria))
    app.add_handler(CommandHandler("capitulos", cmd_capitulos))
    app.add_handler(CommandHandler("personajes", cmd_personajes))
    app.add_handler(CommandHandler("historial", cmd_historial))
    app.add_handler(CommandHandler("resumen", cmd_resumen))
    app.add_handler(CommandHandler("probar", cmd_probar))
    app.add_handler(CommandHandler("preludio_status", cmd_preludio_status))
    app.add_handler(CommandHandler("preludio_preview", cmd_preludio_preview))
    app.add_handler(CommandHandler("preludio_on", cmd_preludio_on))
    app.add_handler(CommandHandler("preludio_off", cmd_preludio_off))
    app.add_handler(CommandHandler("preludio_enviar_hoy", cmd_preludio_enviar_hoy))
    app.add_handler(CommandHandler("inicio_preview", cmd_inicio_preview))
    app.add_handler(CommandHandler("inicio_enviar", cmd_inicio_enviar))
    app.add_handler(CommandHandler("nota", cmd_nota))
    app.add_handler(CommandHandler("corregir_memoria", cmd_corregir_memoria))
    app.add_handler(CommandHandler("pausar", cmd_pausar))
    app.add_handler(CommandHandler("reanudar", cmd_reanudar))
    app.add_handler(CommandHandler("decir", cmd_decir))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_control))
    return app


def build_narrador_app() -> Application:
    app = ApplicationBuilder().token(require_env("TOKEN_NARRADOR")).build()
    app.add_handler(CommandHandler("start", narrador_start))
    app.add_handler(CommandHandler("ayuda", narrador_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_sandra_message))
    return app


async def start_app(app: Application) -> None:
    await app.initialize()
    await app.start()
    if not app.updater:
        raise RuntimeError("La aplicacion de Telegram no tiene updater")
    await app.updater.start_polling()


async def stop_app(app: Application) -> None:
    if app.updater:
        await app.updater.stop()
    await app.stop()
    await app.shutdown()


async def main() -> None:
    global control_app, narrador_app, stop_event

    init_database()
    control_app = build_control_app()
    narrador_app = build_narrador_app()
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    save_data(load_data())
    await start_app(control_app)
    await start_app(narrador_app)
    summary_task = asyncio.create_task(daily_summary_loop())
    logger.info("Control Partida Sandra iniciado")

    try:
        await stop_event.wait()
    finally:
        summary_task.cancel()
        await stop_app(narrador_app)
        await stop_app(control_app)


if __name__ == "__main__":
    asyncio.run(main())
