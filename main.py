import asyncio
import copy
import io
import json
import logging
import os
import signal
import unicodedata
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
from telegram.error import NetworkError, RetryAfter, TimedOut
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


class TelegramTextDeliveryError(RuntimeError):
    def __init__(self, sent_chunks: int, total_chunks: int, cause: Exception):
        super().__init__(str(cause))
        self.sent_chunks = sent_chunks
        self.total_chunks = total_chunks
        self.cause = cause

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
CHAPTER_LORE_DIR = Path("lore/capitulos")
CHAPTER_TITLES = {
    1: "La carta bajo la puerta",
    2: "El Bazar de los Primeros",
    3: "El tren de medianoche",
    4: "Bienvenida a Valdralis",
    5: "La Ceremonia del Umbral",
    6: "La primera clase",
    7: "La noche del pacto",
    8: "El Ala Norte",
    9: "La marca bajo la piel",
    10: "El baile de las tres invitaciones",
    11: "El Sello del Umbral",
}
FINAL_CHAPTER_NUMBER = max(CHAPTER_TITLES)
CHAPTER_PREPARATION_PATHS = {
    2: CHAPTER_LORE_DIR / "02_bazar_de_los_primeros.md",
}
CHAPTER_OPENING_PATHS = {
    2: CHAPTER_LORE_DIR / "02_apertura.md",
}
CHAPTER_SCENE_BEATS = {
    "peligro": "escena de peligro o amenaza",
    "clase_aprendizaje": "escena de clase, entrenamiento o aprendizaje magico",
    "amistad_apoyo": "escena de amistad, ayuda o grupo de apoyo",
    "romance_tension": "escena romantica o tension emocional/sensual",
    "misterio": "pista del misterio principal o de Elara/Umbral",
    "decision": "momento de decision activa de Sandra",
}
CHAPTER_SCENE_MINIMUM_COMPLETED = 4
PROTECTED_CHAPTER_STATE_FIELDS = {
    "chapter",
    "current_chapter_number",
    "completed_chapters",
    "season_complete",
}
HELP_REQUEST_MARKERS = (
    "ayuda",
    "ayudame",
    "no se que hacer",
    "que hago",
    "estoy perdida",
    "estoy bloqueada",
    "recuerdame",
    "hazme un resumen",
    "dame una pista",
    "que opciones tengo",
    "por donde sigo",
)
FORBIDDEN_NARRATOR_PHRASES = (
    "como inteligencia artificial",
    "como ia",
    "soy una ia",
    "soy un bot",
    "soy un modelo",
    "modelo de lenguaje",
    "como asistente",
    "mi prompt",
    "el prompt del sistema",
    "mis instrucciones internas",
    "segun mis instrucciones",
    "politica de contenido",
    "no puedo generar",
    "no puedo proporcionar",
    "no puedo cumplir",
    "no tengo acceso a",
    "fuera de rol",
    "offrol",
    "contacta con miguel",
    "miguel revisara",
)
EXPORT_STORY_ROLES = ("Sandra", "Narrador", "Narrador manual")
CHAPTER_REQUIRED_EVENTS = {
    1: [
        ("la_casa_jaula", "La casa se convierte en una jaula", "Dario sigue siendo una amenaza cercana y Sandra siente la urgencia real de escapar."),
        ("cerradura_responde", "La cerradura responde a Sandra", "La magia de Sandra altera la cerradura o deja una mentira de Dario visible; es su primer desborde."),
        ("la_carta_llama", "La carta llama desde abajo", "La carta obliga a Sandra a actuar mediante un fenomeno imposible, no solo una descripcion."),
        ("encuentra_la_carta", "Sandra alcanza la carta", "Sandra llega a la carta por una accion propia: salir, usar la ventana, enganar a Dario o encontrar otra via."),
        ("kilnip_despierta", "Kilnip despierta", "Kilnip sale del sello azul, se posa en Sandra y establece su primer vinculo protector."),
        ("revelacion_practica", "La carta revela el camino", "Sandra conoce la admision, los materiales y el primer destino: Bazar de los Primeros o estacion imposible."),
        ("dario_casi_descubre", "Dario casi descubre la verdad", "Dario interrumpe, escucha algo o sube; Sandra debe ocultar, mentir, enfrentarse o huir."),
        ("decision_irreversible", "Sandra elige su salida", "Sandra toma una decision activa e irreversible contra una norma de Dario o a favor de Valdralis."),
        ("cruza_el_umbral", "Sandra deja atras la casa", "Sandra abandona la casa o cruza su umbral rumbo al mundo magico. Solo entonces puede abrirse el capitulo 2."),
    ],
    2: [
        ("bazar_respira", "El Bazar se convierte en un lugar vivo", "Sandra recibe una orientacion sensorial real, observa el recinto y puede reaccionar antes de que empiecen las compras."),
        ("elige_a_quien_preguntar", "Sandra elige a quien preguntar", "Kilnip plantea compras, dinero y tren; Sandra escoge por iniciativa propia a uno de los alumnos visibles y juega su respuesta, sea ayuda o rechazo."),
        ("primera_amistad", "Un amigo promete ir con Sandra", "Nora, Izan, Mara o Theo ayuda de verdad a Sandra y acuerda reunirse con ella para ir juntos a la estacion."),
        ("cuenta_valmorien", "Sandra abre la cuenta de Elara", "En el banco, Veyr no basta; Sandra llega voluntariamente a Valmorien, descubre las cinco anualidades y retira dinero."),
        ("uniforme_adquirido", "Sandra elige y compra su uniforme", "La Septima Costura ofrece una escena propia, una decision personal sobre el uniforme y una compra pagada."),
        ("foco_adquirido", "Sandra elige su foco arcano", "Sandra examina alternativas, decide que hacer con la aguja plateada y adquiere conscientemente un foco."),
        ("libros_adquiridos", "Sandra consigue los libros", "El Lomo Despierto plantea un problema jugable que Sandra resuelve antes de comprar el paquete de primer curso."),
        ("baul_adquirido", "Sandra escoge un baul", "Sandra explora las reglas interiores de un baul, resuelve su incidente y lo compra."),
        ("equipo_adquirido", "Sandra consigue el equipo practico", "La Ultima Vela ofrece una situacion con tinta, guantes, velas o libreta; Sandra participa y compra el estuche."),
        ("brujula_adquirida", "Sandra configura su brujula de umbrales", "La Rosa sin Norte plantea una prueba de orientacion; Sandra elige una afinidad, recupera una salida y compra su brujula."),
        ("billete_adquirido", "Sandra obtiene el billete", "Sandra decide el nombre del billete, lo paga y conoce el punto de encuentro hacia la estacion."),
        ("incidente_bazar_resuelto", "Sandra resuelve el incidente del Bazar", "La aguja, un recibo o una alarma de propiedad obliga a Sandra a defender una decision y deja una consecuencia."),
        ("regresa_con_su_aliado", "Sandra regresa por su companero", "Con dinero, seis objetos escolares y billete, Sandra vuelve por decision propia al amigo acordado y ambos empiezan el camino hacia la estacion."),
    ],
}
CHAPTER_EVENT_ORDER_MODE = {1: "sequential", 2: "free"}
CHAPTER_EVENT_PREREQUISITES = {
    2: {
        "elige_a_quien_preguntar": ("bazar_respira",),
        "primera_amistad": ("elige_a_quien_preguntar",),
        "cuenta_valmorien": ("elige_a_quien_preguntar",),
        "uniforme_adquirido": ("elige_a_quien_preguntar",),
        "foco_adquirido": ("elige_a_quien_preguntar",),
        "libros_adquiridos": ("elige_a_quien_preguntar",),
        "baul_adquirido": ("elige_a_quien_preguntar",),
        "equipo_adquirido": ("elige_a_quien_preguntar",),
        "brujula_adquirida": ("elige_a_quien_preguntar",),
        "billete_adquirido": ("elige_a_quien_preguntar",),
        "incidente_bazar_resuelto": ("foco_adquirido",),
        "regresa_con_su_aliado": (
            "primera_amistad",
            "cuenta_valmorien",
            "uniforme_adquirido",
            "foco_adquirido",
            "libros_adquiridos",
            "baul_adquirido",
            "equipo_adquirido",
            "brujula_adquirida",
            "billete_adquirido",
            "incidente_bazar_resuelto",
        ),
    }
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


def normalized_for_detection(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(text).casefold())
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def is_help_request(text: str) -> bool:
    normalized = normalized_for_detection(text)
    return any(marker in normalized for marker in HELP_REQUEST_MARKERS)


def narrator_role_violation(reply: str) -> str | None:
    normalized = normalized_for_detection(reply)
    return next((phrase for phrase in FORBIDDEN_NARRATOR_PHRASES if phrase in normalized), None)


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
        "Vera Ordel": character_sheet(
            "Aun no aparece; futura rival adulta de primer curso, heredera de un linaje de brujas antiguas",
            secretos_que_sabe=[
                "Conoce jerarquias, familias y prejuicios de Valdralis antes de empezar el curso",
            ],
            tension_romantica="No aplica; rivalidad academica y presion social",
            no_revelar_todavia=[
                "No convertirla en una enemiga plana; puede respetar a Sandra si demuestra criterio",
            ],
        ),
        "Gael Voss": character_sheet(
            "Aun no aparece; futuro rival adulto de primer curso, humano tocado por pactos fae",
            secretos_que_sabe=[
                "Sabe formular favores ambiguos y moverse por las pequenas deudas del Bazar",
            ],
            tension_romantica="No aplica; rivalidad, favores y verdades incompletas",
            no_revelar_todavia=[
                "No explicar pronto que pacto fae marco a su familia",
            ],
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
            "Aun no aparece; licantropo adulto de veinte anos, alumno de primer curso, intenso, fisico, protector y desconfiado de la autoridad",
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
            "Aun no aparece; fae adulto joven de primer curso, hermoso, ambiguo y peligroso, criado entre cortes antiguas",
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


def default_scene_beat() -> dict[str, str]:
    return {
        "status": "pendiente",
        "evidence": "",
        "last_update": "",
    }


def normalize_scene_beat(beat: Any) -> dict[str, str]:
    normalized = default_scene_beat()
    if isinstance(beat, str):
        normalized["status"] = beat.strip() or "pendiente"
        return normalized
    if isinstance(beat, dict):
        normalized.update(
            {
                "status": str(beat.get("status") or "pendiente").strip(),
                "evidence": str(beat.get("evidence") or "").strip(),
                "last_update": str(beat.get("last_update") or "").strip(),
            }
        )
    if normalized["status"] not in {"pendiente", "cumplido", "no_aplica"}:
        normalized["status"] = "pendiente"
    return normalized


def normalize_scene_beat_update(beat: Any) -> dict[str, str]:
    if isinstance(beat, str):
        return {"status": beat.strip() or "pendiente"}
    if not isinstance(beat, dict):
        return {}
    update: dict[str, str] = {}
    for field in ("status", "evidence", "last_update"):
        if field in beat:
            update[field] = str(beat.get(field) or "").strip()
    if update.get("status") and update["status"] not in {"pendiente", "cumplido", "no_aplica"}:
        update["status"] = "pendiente"
    return update


def default_chapter_scene_progress() -> dict[str, dict[str, Any]]:
    return {
        str(number): {
            "chapter": chapter_label(number),
            "minimum_completed": CHAPTER_SCENE_MINIMUM_COMPLETED,
            "beats": {
                key: {
                    **default_scene_beat(),
                    "label": label,
                }
                for key, label in CHAPTER_SCENE_BEATS.items()
            },
        }
        for number in CHAPTER_TITLES
    }


def default_required_event() -> dict[str, str]:
    return {"status": "pendiente", "evidence": "", "last_update": ""}


def default_required_event_progress() -> dict[str, dict[str, Any]]:
    return {
        str(chapter_number): {
            "chapter": chapter_label(chapter_number),
            "events": {
                event_key: {
                    **default_required_event(),
                    "label": label,
                    "requirement": requirement,
                }
                for event_key, label, requirement in events
            },
        }
        for chapter_number, events in CHAPTER_REQUIRED_EVENTS.items()
    }


def normalize_required_event_update(event: Any) -> dict[str, str]:
    if isinstance(event, str):
        return {"status": event.strip() or "pendiente"}
    if not isinstance(event, dict):
        return {}
    update: dict[str, str] = {}
    for field in ("status", "evidence", "last_update"):
        if field in event:
            update[field] = str(event.get(field) or "").strip()
    if update.get("status") and update["status"] not in {"pendiente", "cumplido"}:
        update["status"] = "pendiente"
    return update


def merge_required_event_progress(existing: Any, updates: Any = None) -> dict[str, dict[str, Any]]:
    merged = default_required_event_progress()
    for source, partial in (("existing", existing), ("updates", updates)):
        if not isinstance(partial, dict):
            continue
        newly_completed = False
        for chapter_number, chapter_data in partial.items():
            key = str(chapter_number).strip()
            if key not in merged or not isinstance(chapter_data, dict):
                continue
            events = chapter_data.get("events")
            if not isinstance(events, dict):
                continue
            for event_key, event_data in events.items():
                event_key = str(event_key).strip()
                if event_key not in merged[key]["events"]:
                    continue
                update = normalize_required_event_update(event_data)
                current = merged[key]["events"][event_key]
                if source == "updates":
                    incoming_status = update.get("status")
                    if current.get("status") == "cumplido" and incoming_status != "cumplido":
                        update.pop("status", None)
                    elif incoming_status == "cumplido" and current.get("status") != "cumplido":
                        if not update.get("evidence"):
                            update.pop("status", None)
                            incoming_status = None
                        chapter_number_int = int(key)
                        mode = CHAPTER_EVENT_ORDER_MODE.get(chapter_number_int, "sequential")
                        prerequisites = CHAPTER_EVENT_PREREQUISITES.get(
                            chapter_number_int, {}
                        ).get(event_key, ())
                        prerequisites_complete = all(
                            merged[key]["events"].get(required_key, {}).get("status") == "cumplido"
                            for required_key in prerequisites
                        )
                        if mode == "sequential":
                            event_order = list(merged[key]["events"])
                            event_index = event_order.index(event_key)
                            prerequisites_complete = prerequisites_complete and all(
                                merged[key]["events"][earlier_key].get("status") == "cumplido"
                                for earlier_key in event_order[:event_index]
                            )
                        if incoming_status != "cumplido" or newly_completed or not prerequisites_complete:
                            update.pop("status", None)
                        else:
                            newly_completed = True
                merged[key]["events"][event_key] = {
                    **current,
                    **update,
                }
    return merged


def next_required_event(progress: Any, chapter_number: int) -> dict[str, str] | None:
    chapter = merge_required_event_progress(progress).get(str(chapter_number))
    if not chapter:
        return None
    for event_key, event in chapter["events"].items():
        prerequisites = CHAPTER_EVENT_PREREQUISITES.get(chapter_number, {}).get(event_key, ())
        prerequisites_complete = all(
            chapter["events"].get(required_key, {}).get("status") == "cumplido"
            for required_key in prerequisites
        )
        if event.get("status") != "cumplido" and prerequisites_complete:
            return {"key": event_key, **event}
    return None


def chapter_required_events_ready(state: dict[str, Any], chapter_number: int) -> bool:
    if chapter_number not in CHAPTER_REQUIRED_EVENTS:
        return True
    chapter = merge_required_event_progress(state.get("required_event_progress")).get(str(chapter_number), {})
    events = chapter.get("events") if isinstance(chapter, dict) else {}
    return bool(events) and all(event.get("status") == "cumplido" for event in events.values())


def merge_chapter_scene_progress(existing: Any, updates: Any = None) -> dict[str, dict[str, Any]]:
    merged = default_chapter_scene_progress()
    for source, partial in (("existing", existing), ("updates", updates)):
        if not isinstance(partial, dict):
            continue
        for chapter_number, chapter_data in partial.items():
            key = str(chapter_number).strip()
            if key not in merged or not isinstance(chapter_data, dict):
                continue
            if source == "existing" and chapter_data.get("chapter"):
                merged[key]["chapter"] = str(chapter_data.get("chapter")).strip()
            if source == "existing" and chapter_data.get("minimum_completed"):
                try:
                    merged[key]["minimum_completed"] = max(
                        CHAPTER_SCENE_MINIMUM_COMPLETED,
                        min(len(CHAPTER_SCENE_BEATS), int(chapter_data.get("minimum_completed"))),
                    )
                except (TypeError, ValueError):
                    pass
            beats = chapter_data.get("beats")
            if not isinstance(beats, dict):
                continue
            for beat_key, beat_data in beats.items():
                beat_key = str(beat_key).strip()
                if beat_key not in CHAPTER_SCENE_BEATS:
                    continue
                current = merged[key]["beats"][beat_key]
                if source == "existing":
                    merged[key]["beats"][beat_key] = {
                        **current,
                        **normalize_scene_beat(beat_data),
                        "label": CHAPTER_SCENE_BEATS[beat_key],
                    }
                else:
                    update = normalize_scene_beat_update(beat_data)
                    current_status = current.get("status")
                    incoming_status = update.get("status")
                    if current_status in {"cumplido", "no_aplica"} and incoming_status != current_status:
                        update.pop("status", None)
                    elif incoming_status in {"cumplido", "no_aplica"} and not update.get("evidence"):
                        update.pop("status", None)
                    merged[key]["beats"][beat_key] = {
                        **current,
                        **update,
                        "label": CHAPTER_SCENE_BEATS[beat_key],
                    }
    return merged


def chapter_progress_counts(chapter_progress: dict[str, Any]) -> tuple[int, int, int]:
    beats = chapter_progress.get("beats") if isinstance(chapter_progress, dict) else {}
    if not isinstance(beats, dict):
        return (0, 0, len(CHAPTER_SCENE_BEATS))
    completed = sum(1 for beat in beats.values() if normalize_scene_beat(beat)["status"] == "cumplido")
    not_applicable = sum(1 for beat in beats.values() if normalize_scene_beat(beat)["status"] == "no_aplica")
    pending = len(CHAPTER_SCENE_BEATS) - completed - not_applicable
    return completed, not_applicable, pending


def chapter_ready_by_scene_progress(state: dict[str, Any], chapter_number: int) -> bool:
    progress = merge_chapter_scene_progress(state.get("chapter_scene_progress"))
    chapter = progress.get(str(chapter_number), {})
    completed, not_applicable, _pending = chapter_progress_counts(chapter)
    minimum = int(chapter.get("minimum_completed") or CHAPTER_SCENE_MINIMUM_COMPLETED)
    return completed >= minimum or completed + not_applicable >= len(CHAPTER_SCENE_BEATS)


def normalize_state(state: Any) -> dict[str, Any]:
    normalized = default_state()
    if isinstance(state, dict):
        normalized.update(state)
    normalized["character_sheets"] = merge_character_sheets(
        normalized.get("character_sheets")
    )
    normalized["chapter_scene_progress"] = merge_chapter_scene_progress(
        normalized.get("chapter_scene_progress")
    )
    normalized["required_event_progress"] = merge_required_event_progress(
        normalized.get("required_event_progress")
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
    merged["chapter_scene_progress"] = merge_chapter_scene_progress(
        previous.get("chapter_scene_progress"),
        updates.get("chapter_scene_progress"),
    )
    merged["required_event_progress"] = merge_required_event_progress(
        previous.get("required_event_progress"),
        updates.get("required_event_progress"),
    )
    return normalize_state(merged)


def narrative_state_update(scene_state: Any, current_chapter_number: int | None = None) -> dict[str, Any]:
    if not isinstance(scene_state, dict):
        return {}
    update = {
        key: value
        for key, value in scene_state.items()
        if key not in PROTECTED_CHAPTER_STATE_FIELDS
    }
    if current_chapter_number:
        current_key = str(current_chapter_number)
        for progress_field in ("chapter_scene_progress", "required_event_progress"):
            progress = update.get(progress_field)
            if isinstance(progress, dict):
                update[progress_field] = {
                    current_key: progress[current_key]
                } if current_key in progress else {}
    return update


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
        "chapter_scene_progress": default_chapter_scene_progress(),
        "required_event_progress": default_required_event_progress(),
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
        "sent_chapter_openings": [],
        "chapter_review_pause": None,
        "pending_narrator_delivery": None,
        "last_narrator_resend_attempt": None,
        "last_control_error": None,
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
                        chapter_number integer,
                        created_at timestamptz not null default now()
                    )
                    """
                )
                cur.execute(
                    "alter table story_messages add column if not exists chapter_number integer"
                )
                cur.execute(
                    """
                    create index if not exists story_messages_created_at_idx
                    on story_messages (created_at desc, id desc)
                    """
                )
                cur.execute(
                    """
                    create index if not exists story_messages_chapter_idx
                    on story_messages (chapter_number, created_at, id)
                    """
                )
                cur.execute(
                    """
                    create table if not exists chapter_summaries (
                        chapter_number integer primary key,
                        title text not null,
                        summary text not null,
                        state_snapshot jsonb,
                        created_at timestamptz not null default now(),
                        updated_at timestamptz not null default now()
                    )
                    """
                )
                cur.execute(
                    "alter table chapter_summaries add column if not exists state_snapshot jsonb"
                )
                cur.execute(
                    """
                    with story_start as (
                        select min(created_at) as started_at
                        from story_messages
                        where role = 'Narrador'
                          and text like 'Sandra, feliz cumple%'
                    )
                    update story_messages as message
                    set chapter_number = 1
                    from story_start
                    where message.chapter_number is null
                      and story_start.started_at is not null
                      and message.created_at >= story_start.started_at
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
                    seed_chapter_number: int | None = None
                    for item in seed.get("history", []):
                        role = str(item.get("role", "desconocido"))
                        text = str(item.get("text", "")).strip()
                        if text:
                            if role == "Narrador" and text.startswith("Sandra, feliz cumple"):
                                seed_chapter_number = 1
                            chapter_number = item.get("chapter_number")
                            if chapter_number is None:
                                chapter_number = seed_chapter_number
                            cur.execute(
                                "insert into story_messages (role, text, chapter_number) values (%s, %s, %s)",
                                (role, text, chapter_number),
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
    data = {**data, "state": normalize_state(data.get("state"))}
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


def chapter_scene_progress_markdown(progress: Any, chapter_number: int | None = None) -> str:
    normalized = merge_chapter_scene_progress(progress)
    chapter_numbers = [chapter_number] if chapter_number else sorted(CHAPTER_TITLES)
    lines: list[str] = []
    for number in chapter_numbers:
        chapter = normalized.get(str(number))
        if not chapter:
            continue
        completed, not_applicable, pending = chapter_progress_counts(chapter)
        lines.append(f"### {chapter.get('chapter', chapter_label(number))}")
        lines.append(
            f"- Cumplidas: {completed} | No aplica: {not_applicable} | Pendientes: {pending} | Minimo: {chapter.get('minimum_completed', CHAPTER_SCENE_MINIMUM_COMPLETED)}"
        )
        beats = chapter.get("beats") if isinstance(chapter.get("beats"), dict) else {}
        for beat_key in CHAPTER_SCENE_BEATS:
            beat = normalize_scene_beat(beats.get(beat_key))
            label = CHAPTER_SCENE_BEATS[beat_key]
            evidence = f" - {beat['evidence']}" if beat.get("evidence") else ""
            lines.append(f"- {label}: {beat['status']}{evidence}")
        lines.append("")
    return "\n".join(lines).strip() or "- Pendiente"


def required_event_progress_markdown(progress: Any, chapter_number: int | None = None) -> str:
    normalized = merge_required_event_progress(progress)
    chapter_numbers = [chapter_number] if chapter_number else sorted(CHAPTER_REQUIRED_EVENTS)
    lines: list[str] = []
    for number in chapter_numbers:
        chapter = normalized.get(str(number))
        if not chapter:
            continue
        events = chapter.get("events") if isinstance(chapter.get("events"), dict) else {}
        completed = sum(1 for event in events.values() if event.get("status") == "cumplido")
        lines.append(f"### {chapter.get('chapter', chapter_label(number))}")
        lines.append(f"- Hitos resueltos: {completed}/{len(events)}. Todos son obligatorios para cerrar este capitulo.")
        for event in events.values():
            evidence = f" - {event['evidence']}" if event.get("evidence") else ""
            lines.append(f"- {event['label']}: {event.get('status', 'pendiente')}{evidence}")
        next_event = next_required_event(normalized, number)
        if next_event:
            qualifier = "Siguiente hito" if CHAPTER_EVENT_ORDER_MODE.get(number) == "sequential" else "Hito disponible"
            lines.append(f"- {qualifier}: {next_event['label']} ({next_event['requirement']})")
        lines.append("")
    return "\n".join(lines).strip() or "- No hay hitos guiados para este capitulo."


def chapter_label(number: int) -> str:
    title = CHAPTER_TITLES.get(number)
    return f"Capítulo {number}: {title}" if title else f"Capítulo {number}"


def course_complete_reply() -> str:
    return (
        "El primer curso ha terminado.\n\n"
        "Valdralis cierra sus puertas por ahora. Hay promesas que aun no se han roto, "
        "nombres que nadie se atreve a decir en voz alta y miradas que quedaron demasiado "
        "cerca de convertirse en algo mas.\n\n"
        "Cuando llegue el curso que viene, la carta volvera a moverse."
    )


def activate_chapter_review_pause(data: dict[str, Any], completed_chapter: int) -> str:
    if completed_chapter == 1:
        data["chapter_review_pause"] = {
            "active": True,
            "completed_chapter": completed_chapter,
            "next_chapter": 2,
            "until_date": None,
            "requires_manual_resume": True,
            "created_at": now_iso(),
        }
        return "hasta que Miguel la reanude"
    if CHAPTER_REVIEW_PAUSE_DAYS <= 0 or completed_chapter >= FINAL_CHAPTER_NUMBER:
        return ""
    until_date = datetime.now(APP_TIMEZONE).date() + timedelta(days=CHAPTER_REVIEW_PAUSE_DAYS)
    data["chapter_review_pause"] = {
        "active": True,
        "completed_chapter": completed_chapter,
        "next_chapter": min(FINAL_CHAPTER_NUMBER, completed_chapter + 1),
        "until_date": until_date.isoformat(),
        "requires_manual_resume": False,
        "created_at": now_iso(),
    }
    return until_date.isoformat()


def chapter_review_pause_is_active(data: dict[str, Any]) -> bool:
    pause = data.get("chapter_review_pause")
    if not isinstance(pause, dict) or not pause.get("active"):
        return False
    if pause.get("requires_manual_resume"):
        return True
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
    if str(completed) == "1":
        return (
            "CAPÍTULO 1 TERMINADO\n\n"
            "Has alcanzado el primer hito de Valdralis. La historia queda en pausa por ahora.\n\n"
            "Pronto recibirás la continuación."
        )
    until_date = pause.get("until_date")
    chapter = chapter_label(int(completed)) if str(completed).isdigit() else "El capitulo"
    return (
        f"{chapter} ya ha cerrado sus puertas.\n\n"
        "Valdralis guarda silencio entre umbrales. Algunas historias necesitan reposar "
        "antes de abrir la siguiente puerta.\n\n"
        f"Cuando la niebla vuelva a levantarse, la historia continuara."
        + (f"\n\nFecha prevista: {until_date}." if until_date else "")
    )


def open_pending_chapter_after_review(data: dict[str, Any]) -> int | None:
    pause = data.get("chapter_review_pause")
    if not isinstance(pause, dict):
        return None
    try:
        completed = int(pause.get("completed_chapter") or 0)
        next_chapter = int(pause.get("next_chapter") or completed + 1)
        current = int((data.get("state") or {}).get("current_chapter_number") or 0)
    except (TypeError, ValueError):
        return None
    if completed != 1 or current != completed or next_chapter != completed + 1:
        return None
    state = data.setdefault("state", default_state())
    state["current_chapter_number"] = next_chapter
    state["chapter"] = chapter_label(next_chapter)
    state["season_complete"] = False
    state["location"] = "Galeria del Umbral del Bazar de los Primeros, ante la tienda de focos"
    state["current_scene"] = (
        "La aguja plateada acaba de mostrar una puerta, siete sombras y una mujer parecida "
        "a Sandra; Orla Nadir espera que Sandra diga que apellido pretende cobrarle"
    )
    state["next_suggested_scene"] = (
        "Esperar la respuesta de Sandra a Orla y abrir despues el Bazar como un recinto vivo "
        "que pueda explorar"
    )
    return next_chapter


def apply_chapter_transition(data: dict[str, Any], transition: Any) -> str:
    if not isinstance(transition, dict) or not transition.get("completed"):
        return ""

    state = data.setdefault("state", default_state())
    try:
        completed = int(transition.get("completed_chapter") or state.get("current_chapter_number") or 0)
    except (TypeError, ValueError):
        completed = 0
    if completed < 1 or completed > FINAL_CHAPTER_NUMBER:
        return ""

    completed_chapters = {
        int(number)
        for number in (state.get("completed_chapters") or [])
        if str(number).isdigit()
    }
    completed_chapters.add(completed)
    state["completed_chapters"] = sorted(completed_chapters)

    if completed >= FINAL_CHAPTER_NUMBER:
        state["current_chapter_number"] = FINAL_CHAPTER_NUMBER
        state["chapter"] = chapter_label(FINAL_CHAPTER_NUMBER)
        state["season_complete"] = True
        state["current_scene"] = "Primer curso terminado"
        state["next_suggested_scene"] = "Esperar al curso que viene"
        return (
            f"{chapter_label(FINAL_CHAPTER_NUMBER)} terminado.\n\n"
            "Primer curso terminado.\n\n"
            "La historia se detiene aqui, por ahora. Valdralis volvera a abrir sus puertas el curso que viene."
        )

    if completed == 1:
        state["current_chapter_number"] = 1
        state["chapter"] = chapter_label(1)
        state["season_complete"] = False
        state["current_scene"] = "Capítulo 1 terminado; el siguiente umbral permanece cerrado"
        state["next_suggested_scene"] = "Esperar a que vuelva a abrirse el camino hacia Valdralis"
        return ""

    next_chapter = min(FINAL_CHAPTER_NUMBER, completed + 1)
    state["current_chapter_number"] = next_chapter
    state["chapter"] = chapter_label(next_chapter)
    state["season_complete"] = False
    return f"{chapter_label(completed)} terminado.\n\n{chapter_label(next_chapter)}"


def completed_chapter_from_transition(state: dict[str, Any], transition: Any) -> int | None:
    if not isinstance(transition, dict) or not transition.get("completed"):
        return None
    try:
        completed = int(transition.get("completed_chapter") or state.get("current_chapter_number") or 0)
        current = int(state.get("current_chapter_number") or 0)
    except (TypeError, ValueError):
        return None
    if completed < 1 or completed > FINAL_CHAPTER_NUMBER or completed != current:
        return None
    proposed_next = transition.get("next_chapter")
    if completed < FINAL_CHAPTER_NUMBER and proposed_next is not None:
        try:
            if int(proposed_next) != completed + 1:
                return None
        except (TypeError, ValueError):
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

## Progreso de escenas por capitulo

{chapter_scene_progress_markdown(state.get('chapter_scene_progress') or {})}

## Hitos narrativos obligatorios

{required_event_progress_markdown(state.get('required_event_progress') or {})}

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


def append_history(role: str, text: str, *, chapter_number: int | None = None) -> None:
    data = load_data()
    if chapter_number is None:
        try:
            inferred_chapter = int((data.get("state") or {}).get("current_chapter_number") or 0)
        except (TypeError, ValueError):
            inferred_chapter = 0
        chapter_number = inferred_chapter if 1 <= inferred_chapter <= FINAL_CHAPTER_NUMBER else None
    if db_enabled():
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "insert into story_messages (role, text, chapter_number) values (%s, %s, %s)",
                    (role, text, chapter_number),
                )
    data.setdefault("history", []).append(
        {
            "role": role,
            "text": text,
            "at": now_iso(),
            "chapter_number": chapter_number,
        }
    )
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


def read_chapter_preparation(chapter_number: int) -> str:
    path = CHAPTER_PREPARATION_PATHS.get(chapter_number)
    if not path:
        return "No hay un documento adicional para este capitulo; seguir la Biblia general."
    if not path.exists():
        return f"Falta la preparacion esperada para {chapter_label(chapter_number)}."
    return path.read_text(encoding="utf-8")


def read_chapter_opening(chapter_number: int) -> str:
    path = CHAPTER_OPENING_PATHS.get(chapter_number)
    if not path or not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def chapter_scene_prompt_schema(chapter_number: int) -> str:
    beats = {
        key: {
            "status": "pendiente|cumplido|no_aplica",
            "evidence": "que escena lo cumplio",
            "last_update": "turno actual breve",
        }
        for key in CHAPTER_SCENE_BEATS
    }
    return json.dumps(
        {
            str(chapter_number): {
                "chapter": chapter_label(chapter_number),
                "minimum_completed": CHAPTER_SCENE_MINIMUM_COMPLETED,
                "beats": beats,
            }
        },
        ensure_ascii=False,
        indent=2,
    )


def required_event_prompt_schema(chapter_number: int) -> str:
    events = {
        event_key: {
            "status": "pendiente|cumplido",
            "evidence": requirement,
            "last_update": "turno actual breve",
        }
        for event_key, _label, requirement in CHAPTER_REQUIRED_EVENTS.get(chapter_number, [])
    }
    return json.dumps(
        {str(chapter_number): {"events": events}} if events else {},
        ensure_ascii=False,
        indent=2,
    )


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


def save_chapter_summary(
    chapter_number: int,
    title: str,
    summary: str,
    state_snapshot: dict[str, Any] | None = None,
) -> None:
    summary = summary.strip()
    if not summary:
        return

    if db_enabled():
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into chapter_summaries (
                        chapter_number, title, summary, state_snapshot, updated_at
                    )
                    values (%s, %s, %s, %s, now())
                    on conflict (chapter_number) do update
                    set title = excluded.title,
                        summary = excluded.summary,
                        state_snapshot = coalesce(excluded.state_snapshot, chapter_summaries.state_snapshot),
                        updated_at = now()
                    """,
                    (
                        chapter_number,
                        title,
                        summary,
                        Jsonb(state_snapshot) if isinstance(state_snapshot, dict) else None,
                    ),
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
            "state_snapshot": state_snapshot if isinstance(state_snapshot, dict) else None,
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


def chapter_story_messages(chapter_number: int) -> list[dict[str, Any]]:
    if db_enabled():
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select role, text, created_at
                    from story_messages
                    where chapter_number = %s
                      and role = any(%s::text[])
                    order by created_at asc, id asc
                    """,
                    (chapter_number, list(EXPORT_STORY_ROLES)),
                )
                return [
                    {"role": str(role), "text": str(text), "at": created_at}
                    for role, text, created_at in cur.fetchall()
                ]

    history = load_data().get("history", [])
    messages: list[dict[str, Any]] = []
    chapter_one_started = False
    for item in history:
        role = str(item.get("role") or "")
        text = str(item.get("text") or "")
        if role not in EXPORT_STORY_ROLES or not text.strip():
            continue
        item_chapter = item.get("chapter_number")
        try:
            item_chapter_number = int(item_chapter) if item_chapter is not None else None
        except (TypeError, ValueError):
            item_chapter_number = None
        if chapter_number == 1 and role == "Narrador" and text.startswith("Sandra, feliz cumple"):
            chapter_one_started = True
        if item_chapter_number == chapter_number or (
            chapter_number == 1 and item_chapter_number is None and chapter_one_started
        ):
            messages.append({"role": role, "text": text, "at": item.get("at")})
    return messages


def chapter_export_context(chapter_number: int) -> tuple[str, str, dict[str, Any] | None]:
    title = CHAPTER_TITLES.get(chapter_number, f"Capítulo {chapter_number}")
    summary = ""
    snapshot: dict[str, Any] | None = None
    if db_enabled():
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select title, summary, state_snapshot
                    from chapter_summaries
                    where chapter_number = %s
                    """,
                    (chapter_number,),
                )
                row = cur.fetchone()
        if row:
            title = str(row[0] or title)
            summary = str(row[1] or "").strip()
            snapshot = row[2] if isinstance(row[2], dict) else None
    else:
        for item in load_data().get("chapter_summaries", []):
            try:
                item_number = int(item.get("chapter_number", 0))
            except (TypeError, ValueError):
                continue
            if item_number == chapter_number:
                title = str(item.get("title") or title)
                summary = str(item.get("summary") or "").strip()
                raw_snapshot = item.get("state_snapshot")
                snapshot = raw_snapshot if isinstance(raw_snapshot, dict) else None
                break

    if snapshot is None:
        data = load_data()
        current_state = data.get("state") or {}
        try:
            current_chapter = int(current_state.get("current_chapter_number") or 0)
        except (TypeError, ValueError):
            current_chapter = 0
        if current_chapter == chapter_number:
            snapshot = current_state
    return title, summary, snapshot


def export_visible_state_markdown(snapshot: dict[str, Any] | None) -> str:
    if not isinstance(snapshot, dict):
        return "- No hay una memoria de cierre disponible."
    visible_sheets: list[str] = []
    visible_character_names: set[str] = set()
    sheets = snapshot.get("character_sheets")
    all_sheet_names = {str(name) for name in sheets} if isinstance(sheets, dict) else set()
    if isinstance(sheets, dict):
        for name, raw_sheet in sheets.items():
            sheet = normalize_character_sheet(raw_sheet)
            if normalized_for_detection(sheet["ultima_escena_juntos"]).startswith("aun no apare"):
                continue
            visible_character_names.add(str(name))
            visible_sheets.append(
                f"{name}: relacion={sheet['relacion_actual']}; "
                f"ultima escena={sheet['ultima_escena_juntos']}; "
                f"tension={sheet['tension_romantica']}"
            )

    lines = [
        f"- Lugar: {snapshot.get('location') or 'Pendiente'}",
        f"- Escena final: {snapshot.get('current_scene') or 'Pendiente'}",
        "",
        "### Hechos conocidos por Sandra",
        markdown_list(snapshot.get("known_facts") or []),
        "",
        "### Objetos relevantes",
        markdown_list(snapshot.get("inventory") or []),
        "",
        "### Hilos abiertos",
        markdown_list(snapshot.get("open_threads") or []),
        "",
        "### Secretos ya revelados",
        markdown_list(snapshot.get("revealed_secrets") or []),
        "",
        "### Relaciones al cierre",
    ]
    relationships = snapshot.get("relationships")
    if isinstance(relationships, dict) and relationships:
        visible_relationships = [
            f"{name}: {value}"
            for name, value in relationships.items()
            if (
                str(name) in visible_character_names
                or (
                    str(name) not in all_sheet_names
                    and not normalized_for_detection(value).startswith("aun no apare")
                )
            )
        ]
        lines.append(markdown_list(visible_relationships))
    else:
        lines.append("- Pendiente")

    lines.extend(["", "### Fichas visibles de personajes", markdown_list(visible_sheets)])
    return "\n".join(lines)


def format_export_timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(APP_TIMEZONE).strftime("%Y-%m-%d %H:%M")
    raw = str(value or "").strip()
    if not raw:
        return "hora no registrada"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.astimezone(APP_TIMEZONE).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return raw


def build_chapter_export(chapter_number: int) -> tuple[str, int, bool]:
    messages = chapter_story_messages(chapter_number)
    title, summary, snapshot = chapter_export_context(chapter_number)
    generated_at = datetime.now(APP_TIMEZONE).strftime("%Y-%m-%d %H:%M %Z")
    lines = [
        f"# Academia de Valdralis - Capítulo {chapter_number}: {title}",
        "",
        f"Exportado: {generated_at}",
        f"Mensajes: {len(messages)}",
        f"Capítulo cerrado: {'sí' if summary else 'no; exportación provisional'}",
        "",
        "## Instrucciones editoriales para ChatGPT",
        "",
        "Convierte esta transcripción en un capítulo de novela en segunda persona o tercera persona cercana, manteniendo las decisiones, acciones y frases importantes de Sandra. Integra sus mensajes en la prosa, elimina repeticiones propias del chat y conserva la continuidad emocional. No inventes revelaciones que no aparezcan en la transcripción o en el resumen canónico. No adelantes secretos de capítulos posteriores.",
        "",
        "## Resumen canónico",
        "",
        summary or "El capítulo todavía no tiene un resumen canónico de cierre.",
        "",
        "## Memoria visible al cierre",
        "",
        export_visible_state_markdown(snapshot),
        "",
        "## Transcripción completa",
        "",
    ]
    role_labels = {
        "Sandra": "Sandra",
        "Narrador": "Narrador",
        "Narrador manual": "Narrador",
    }
    for index, message in enumerate(messages, start=1):
        role = role_labels.get(str(message.get("role")), str(message.get("role") or "Desconocido"))
        timestamp = format_export_timestamp(message.get("at"))
        lines.extend(
            [
                f"### Mensaje {index} - {role} - {timestamp}",
                "",
                str(message.get("text") or "").strip(),
                "",
            ]
        )
    if not messages:
        lines.append("No hay mensajes etiquetados para este capítulo.")
    return "\n".join(lines).strip() + "\n", len(messages), bool(summary)


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
        "La carta todavía no se abre. Su sello azul palpita una vez bajo tus dedos y vuelve a quedarse quieto.\n\n"
        "Aún no hay nada que resolver. Por ahora, solo puedes guardar las señales y dejar que "
        "Valdralis se acerque. Cuando llegue la noche correcta, la carta te preguntará qué haces."
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


def telegram_retry_seconds(error: Exception, attempt: int) -> float:
    retry_after = getattr(error, "retry_after", None)
    if hasattr(retry_after, "total_seconds"):
        retry_after = retry_after.total_seconds()
    try:
        return max(1.0, min(30.0, float(retry_after)))
    except (TypeError, ValueError):
        return float(min(8, 2 ** attempt))


async def send_telegram_text(
    bot: Any,
    *,
    chat_id: int,
    text: str,
    start_chunk: int = 0,
    max_attempts: int = 3,
) -> int:
    chunks = split_long(text)
    sent_chunks = max(0, min(start_chunk, len(chunks)))
    for chunk_index in range(sent_chunks, len(chunks)):
        chunk = chunks[chunk_index]
        for attempt in range(max_attempts):
            try:
                await bot.send_message(chat_id=chat_id, text=chunk)
                sent_chunks = chunk_index + 1
                break
            except (RetryAfter, TimedOut, NetworkError) as exc:
                if attempt + 1 >= max_attempts:
                    raise TelegramTextDeliveryError(sent_chunks, len(chunks), exc) from exc
                delay = telegram_retry_seconds(exc, attempt)
                logger.warning(
                    "Fallo temporal enviando Telegram, fragmento %s/%s, reintento %s en %.1fs: %s",
                    chunk_index + 1,
                    len(chunks),
                    attempt + 2,
                    delay,
                    type(exc).__name__,
                )
                await asyncio.sleep(delay)
            except Exception as exc:
                raise TelegramTextDeliveryError(sent_chunks, len(chunks), exc) from exc
    return sent_chunks


def latest_narrator_message() -> tuple[str, int | None] | None:
    if db_enabled():
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select text, chapter_number
                    from story_messages
                    where role = 'Narrador'
                    order by created_at desc, id desc
                    limit 1
                    """
                )
                row = cur.fetchone()
                if row:
                    return str(row[0]), int(row[1]) if row[1] is not None else None
    for item in reversed(load_data().get("history", [])):
        if str(item.get("role") or "") != "Narrador":
            continue
        chapter_number = item.get("chapter_number")
        try:
            chapter_number = int(chapter_number) if chapter_number is not None else None
        except (TypeError, ValueError):
            chapter_number = None
        return str(item.get("text") or ""), chapter_number
    return None


async def deliver_pending_narrator_reply() -> bool:
    if not narrador_app:
        return False
    data = load_data()
    pending = data.get("pending_narrator_delivery")
    sandra_id = data.get("sandra_chat_id")
    if not isinstance(pending, dict) or not sandra_id:
        return False
    text = str(pending.get("text") or "").strip()
    if not text:
        return False
    try:
        start_chunk = max(0, int(pending.get("sent_chunks") or 0))
    except (TypeError, ValueError):
        start_chunk = 0
    try:
        await send_telegram_text(
            narrador_app.bot,
            chat_id=int(sandra_id),
            text=text,
            start_chunk=start_chunk,
        )
    except TelegramTextDeliveryError as exc:
        data = load_data()
        current_pending = data.get("pending_narrator_delivery")
        if isinstance(current_pending, dict):
            current_pending["sent_chunks"] = exc.sent_chunks
            current_pending["last_error"] = f"{type(exc.cause).__name__}: {exc.cause}"
            current_pending["last_attempt_at"] = now_iso()
            data["pending_narrator_delivery"] = current_pending
            save_data(data)
        return False

    data = load_data()
    pending = data.get("pending_narrator_delivery")
    if not isinstance(pending, dict):
        return False
    proposed_state = pending.get("state_after_delivery")
    if isinstance(proposed_state, dict):
        data["state"] = proposed_state
    data["chapter_review_pause"] = pending.get("chapter_review_pause_after_delivery")
    data["pending_narrator_delivery"] = None
    save_data(data)
    try:
        chapter_number = int(pending.get("chapter_number"))
    except (TypeError, ValueError):
        chapter_number = None
    append_history("Narrador", text, chapter_number=chapter_number)
    return True


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
    help_request = "si" if is_help_request(sandra_message) else "no"
    current_state = load_data().get("state") or default_state()
    try:
        current_chapter_number = int(current_state.get("current_chapter_number") or 1)
    except (TypeError, ValueError):
        current_chapter_number = 1
    current_chapter_number = max(1, min(FINAL_CHAPTER_NUMBER, current_chapter_number))
    scene_progress_schema = chapter_scene_prompt_schema(current_chapter_number)
    required_progress_schema = required_event_prompt_schema(current_chapter_number)
    completed_chapters_schema = json.dumps(
        current_state.get("completed_chapters") or [],
        ensure_ascii=False,
    )
    season_complete_schema = "true" if current_state.get("season_complete") else "false"
    chapter_preparation = read_chapter_preparation(current_chapter_number)
    required_progress = merge_required_event_progress(current_state.get("required_event_progress"))
    kilnip_event = (
        required_progress.get("1", {})
        .get("events", {})
        .get("kilnip_despierta", {})
    )
    kilnip_awake = "si" if kilnip_event.get("status") == "cumplido" else "no"
    prompt = f"""
Eres el narrador privado de una novela interactiva de fantasia romantica gotica.
La jugadora es Sandra. No eres un asistente: eres la voz de la historia.

REGLAS DE ESTILO:
- Nunca salgas del rol de narrador. No digas que eres IA, bot, modelo, sistema, prompt ni asistente.
- No respondas en offrol a Sandra. Si Sandra pregunta algo tecnico, administrativo o fuera de personaje, reconducelo dentro de la ficcion con una respuesta breve de Valdralis.
- Si detectas offrol, duda de direccion, problema de seguridad narrativa o pregunta para Miguel, marca admin_alert con el aviso. A Sandra no le expliques el funcionamiento interno.
- El campo reply solo puede contener narracion, dialogo de personajes y elementos que existan dentro del mundo. No incluyas encabezados tecnicos, explicaciones de reglas, comentarios al jugador, analisis, disculpas ni menciones al control privado.
- Si Sandra pide ayuda, un resumen, una pista, un recordatorio o dice que no sabe que hacer, responde dentro de la escena mediante Kilnip. Kilnip debe recordarle brevemente hechos que Sandra ya conoce, objetos relevantes, hilos abiertos y la urgencia inmediata, sin revelar secretos pendientes y sin ofrecer opciones A/B/C.
- Pedir ayuda no equivale a realizar una accion. Despues de la pista de Kilnip, no traslades a Sandra, no resuelvas la escena, no abras una zona nueva ni completes un hito salvo que su mismo mensaje contenga tambien una accion que lo justifique.
- Una vez despierto, Kilnip habla directamente dentro de la cabeza de Sandra cuando ella pide ayuda. Deja claro que solo ella oye esa voz. Usa de una a cuatro frases mentales breves, nerviosas y concretas, y acompanalo con algun gesto fisico de Kilnip. Su voz resume y orienta, pero no decide por Sandra.
- Si Kilnip aun no ha salido del sello, no lo hagas aparecer antes de tiempo: la carta, el sello azul o la casa deben ofrecer la pista de forma diegetica. Despues de despertar, Kilnip es siempre la guia principal cuando Sandra se bloquea.
- Narra en segunda persona, en espanol.
- Trata el gesto, frase o accion de Sandra como un hecho que acaba de ocurrir y continua desde su consecuencia. No empieces repitiendo, citando ni parafraseando su mensaje. Solo conserva literalmente las palabras que Sandra haya pronunciado como dialogo cuando resulte natural.
- Sandra es propietaria de su interioridad. Puedes describir reacciones fisicas involuntarias, percepciones, impulsos ambiguos y posibilidades, pero no decidir que perdona, confia, ama, desea, acepta, comprende o supera algo si ella no lo ha expresado.
- Ante una revelacion, una aparicion sobrenatural, una pregunta directa o una eleccion importante, no encadenes automaticamente el siguiente acontecimiento. Deja espacio real para que Sandra observe, pregunte, dude o reaccione.
- Sandra ha vivido toda su vida como humana. Trata cada primera imposibilidad magica con presencia fisica, consecuencias visibles y tiempo para reaccionar; no asumas que la acepta con normalidad ni declares por ella que siente asombro o miedo.
- Mantiene continuidad espacial. Narra puertas, trayectos, distancias y cambios de ambiente cuando importen; no teletransportes a Sandra entre lugares ni resumas una exploracion como una lista de paradas.
- En escenas de descubrimiento describe el mundo mediante detalles concretos de varios sentidos y muestra actividad de fondo que no exista solo para guiar a Sandra.
- Prosa literaria, atmosferica y emocional, pero precisa. Prefiere imagenes concretas a cadenas de comparaciones y no abuses de "parece", "como si", "algo", objetos que respiran o frases que explican el significado emocional de la escena.
- Cada respuesta debe desarrollar un movimiento narrativo principal con profundidad. No comprimas llegada, explicacion, conflicto, solucion y salida en la misma respuesta.
- Los PNJ tienen objetivos, humor, limites y opiniones. Deben preguntar, interrumpir, equivocarse y esperar respuestas; no funcionar como mostradores de exposicion.
- No uses en reply el nombre propio de un personaje hasta que Sandra lo haya oido, leido o averiguado dentro de la historia. Los nombres privados de la Biblia no son conocimiento de Sandra.
- No escribas por Sandra la respuesta a una pregunta de un PNJ. Tras una pregunta importante, una oferta o una provocacion, deja que ella conteste antes de hacer avanzar esa conversacion.
- No uses opciones A/B/C ni menus.
- Tampoco disfraces un menu dentro del dialogo con formulas como "puedes hacer X, Y o Z". Un PNJ puede dar informacion o formular una pregunta, pero no enumerar las posibles respuestas de Sandra.
- No termines con una instruccion de juego como "que haces?". La decision debe quedar abierta por la propia situacion, con estilo de novela.
- Una respuesta ordinaria debe intentar caber en un unico mensaje de Telegram: normalmente entre 1500 y 3500 caracteres. La profundidad nace de una escena concreta, no de acumular varios movimientos. Solo un climax puede necesitar mas extension.
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
- Actualiza state.chapter_scene_progress del capitulo actual. Marca como "cumplido" cualquier beat que haya ocurrido con una evidencia breve. Beats: peligro, clase_aprendizaje, amistad_apoyo, romance_tension, misterio, decision.
- No cierres un capitulo si no se han cumplido al menos 4 de los 6 beats, salvo que los no aplicables esten marcados como "no_aplica" con evidencia. La decision y la pista de misterio casi siempre deben cumplirse antes de cerrar.
- Si el capitulo actual tiene state.required_event_progress, todos sus hitos son obligatorios. Un hito solo se cumple si se ha jugado en la narracion, Sandra ha tenido una oportunidad real de intervenir y existe evidencia concreta. El sistema acepta como maximo un hito nuevo por respuesta.
- En el capitulo 1 los hitos siguen el orden indicado. En el capitulo 2, despues de que el Bazar se haya presentado como lugar vivo y Sandra haya elegido a quien preguntar, ella decide libremente el orden de banco, seis tiendas y taquilla; no la conduzcas automaticamente al siguiente establecimiento. El reencuentro con su aliado siempre es el ultimo hito.
- No cierres el capitulo 1 hasta que TODOS sus hitos obligatorios esten en estado "cumplido". El ultimo, "Sandra deja atras la casa", exige que haya cruzado el umbral de la casa hacia el mundo magico; aceptar la carta sin salir aun no basta.
- No cierres el capitulo 2 hasta que TODOS sus hitos obligatorios esten en estado "cumplido": orientacion, alumno elegido, primera amistad, cuenta Valmorien, seis objetos escolares, billete, incidente resuelto y regreso voluntario al companero de estacion.
- No adelantes state.chapter, state.current_chapter_number, state.completed_chapters ni state.season_complete. El sistema es el unico que cambia de capitulo despues de validar todos los requisitos.
- Mantener y actualizar las fichas vivas de personajes. Si una escena cambia una relacion, secreto, ultima escena, tension romantica o limite de revelacion, actualiza solo esa ficha en state.character_sheets. No inventes cambios para personajes que no han intervenido.
- Cuando termine un capitulo, marca chapter_transition.completed=true, pero no escribas tu el cartel de "Capitulo terminado"; el sistema lo anadira.
- Tras completar el capitulo {FINAL_CHAPTER_NUMBER}, marca season_complete=true y no abras un capitulo {FINAL_CHAPTER_NUMBER + 1}.

BIBLIA DE LA PARTIDA:
{read_lore()}

PREPARACION PRIVADA DEL CAPITULO ACTUAL ({chapter_label(current_chapter_number)}):
{chapter_preparation}

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

PETICION DE AYUDA O RECORDATORIO DETECTADA: {help_request}
KILNIP YA HA DESPERTADO Y ESTA VINCULADO A SANDRA: {kilnip_awake}
Si la peticion pone "si" y Kilnip esta despierto, incluye obligatoriamente su voz dentro de la cabeza de Sandra. Si sigue sellado, la guia debe venir de la carta azul sin hacer aparecer a Kilnip antes de tiempo.

Devuelve SOLO JSON valido con este formato:
{{
  "reply": "escena narrativa completa para Sandra, con la extension que necesite su movimiento principal; siempre dentro de la ficcion",
  "state": {{
    "chapter": "{chapter_label(current_chapter_number)}",
    "current_chapter_number": {current_chapter_number},
    "completed_chapters": {completed_chapters_schema},
    "season_complete": {season_complete_schema},
    "location": "lugar actual",
    "current_scene": "escena actual",
    "known_facts": ["hechos que Sandra ya sabe"],
    "relationships": {{"nombre": "estado breve de relacion"}},
    "chapter_scene_progress": {scene_progress_schema},
    "required_event_progress": {required_progress_schema},
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
    correction = ""
    for attempt in range(2):
        response = await openai_client().responses.create(
            model=OPENAI_MODEL,
            input=prompt + correction,
            text={"format": {"type": "json_object"}},
        )
        if not response.output_text:
            raise RuntimeError("OpenAI devolvio una respuesta vacia")
        data = extract_json(response.output_text)
        reply = str(data.get("reply") or "").strip()
        if not reply:
            raise RuntimeError("La IA no devolvio reply")
        violation = narrator_role_violation(reply)
        if not violation:
            if not isinstance(data.get("state"), dict):
                data["state"] = load_data().get("state", default_state())
            return data
        logger.warning(
            "Respuesta narrativa rechazada por salida de rol (%s), intento %s",
            violation,
            attempt + 1,
        )
        correction = (
            "\n\nCORRECCION OBLIGATORIA: tu respuesta anterior rompio el rol de narrador. "
            "Genera de nuevo todo el JSON. El campo reply debe permanecer por completo dentro "
            "de la ficcion y no puede mencionar IA, bot, modelo, sistema, prompt, asistente, "
            "offrol ni a Miguel."
        )
    raise RuntimeError("La IA insistio en salir del rol de narrador")


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
Incluye solo hechos ocurridos en escena o conocidos por Sandra. No copies secretos pendientes ni informacion que todavia no se haya revelado.

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
        "Control Partida Sandra activo. Usa /status, /estado, /historial 20, "
        "/entrega, /reenviar_ultimo o /nota."
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
    if chapter_review_pause_is_active(data):
        await update.effective_chat.send_message(chapter_review_pause_reply(data))
        return
    if (data.get("state") or {}).get("season_complete"):
        await update.effective_chat.send_message(course_complete_reply())
        return
    if prelude_guard_active():
        await update.effective_chat.send_message(
            "Valdralis está cerca, pero la carta aún no se abre. "
            "Hasta la noche correcta, solo llegarán señales."
        )
        return
    await update.effective_chat.send_message(
        "Una línea de tinta azul se mueve en el margen de la carta, atenta. "
        "Valdralis escucha lo que haces, dices y sientes."
    )


async def narrador_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    data = load_data()
    sandra_id = data.get("sandra_chat_id")
    if sandra_id and int(sandra_id) != update.effective_chat.id:
        return
    if chapter_review_pause_is_active(data):
        await update.effective_chat.send_message(chapter_review_pause_reply(data))
        return
    if (data.get("state") or {}).get("season_complete"):
        await update.effective_chat.send_message(course_complete_reply())
        return
    if prelude_guard_active():
        await update.effective_chat.send_message(prelude_reply_for_text("ayuda"))
        return
    chat_id = update.effective_chat.id
    help_text = (
        "Me detengo y pido una señal. Necesito recordar qué sé, qué asuntos siguen abiertos "
        "y qué es lo más urgente ahora."
    )
    sandra_message_buffers.setdefault(chat_id, []).append(help_text)
    existing_task = sandra_message_tasks.get(chat_id)
    if existing_task and not existing_task.done():
        existing_task.cancel()
    sandra_message_tasks[chat_id] = asyncio.create_task(process_sandra_message_after_idle(chat_id))
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)


async def unknown_narrador_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await narrador_help(update, context)


async def process_sandra_message_after_idle(chat_id: int) -> None:
    try:
        await asyncio.sleep(MESSAGE_BUFFER_SECONDS)
        sandra_message_tasks.pop(chat_id, None)
        await process_sandra_message_batch(chat_id)
    except asyncio.CancelledError:
        return
    except Exception as exc:
        sandra_message_tasks.pop(chat_id, None)
        logger.exception("Fallo inesperado procesando el lote de Sandra")
        try:
            await send_admin(
                f"Fallo inesperado procesando el mensaje de Sandra: {type(exc).__name__}: {exc}"
            )
        except Exception:
            logger.exception("No se pudo avisar al chat de control del fallo inesperado")


async def process_sandra_message_batch(chat_id: int) -> None:
    if not narrador_app:
        return

    messages = sandra_message_buffers.pop(chat_id, [])
    sandra_message_tasks.pop(chat_id, None)
    clean_messages = [message.strip() for message in messages if message.strip()]
    if not clean_messages:
        return

    text = "\n".join(clean_messages)
    data = load_data()
    if data.get("paused"):
        await narrador_app.bot.send_message(
            chat_id=chat_id,
            text="El reloj sin minutos ha detenido sus agujas. Valdralis guarda silencio.",
        )
        return

    if isinstance(data.get("pending_narrator_delivery"), dict):
        delivered = await deliver_pending_narrator_reply()
        try:
            await send_admin(
                "Sandra escribio mientras habia una respuesta anterior pendiente de entrega. "
                + (
                    "He entregado primero la respuesta pendiente; no he enviado su nuevo texto a la IA ni lo he guardado como accion."
                    if delivered
                    else "Telegram sigue sin confirmar la entrega; no he enviado su nuevo texto a la IA ni lo he guardado como accion."
                )
                + f"\n\nTexto recibido fuera de continuidad:\n{text}"
            )
        except Exception:
            logger.exception("No se pudo avisar del mensaje recibido durante una entrega pendiente")
        return

    if chapter_review_pause_is_active(data):
        await narrador_app.bot.send_message(chat_id=chat_id, text=chapter_review_pause_reply(data))
        await send_admin(
            "Sandra ha escrito durante un cierre de revision de capitulo. "
            "He enviado el aviso fijo de hito alcanzado; no he llamado a la IA ni he guardado el mensaje en la memoria narrativa.\n\n"
            f"Sandra:\n{text}"
        )
        return

    try:
        turn_chapter_number = int((data.get("state") or {}).get("current_chapter_number") or 0) or None
    except (TypeError, ValueError):
        turn_chapter_number = None
    append_history("Sandra", text, chapter_number=turn_chapter_number)
    await send_admin(f"Sandra ({len(clean_messages)} mensaje/s agrupado/s):\n{text}")

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
            text="La tinta azul se queda inmóvil. Algo al otro lado de la carta contiene el aliento.",
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
            text="La tinta azul se quiebra a mitad de una palabra. Durante un instante, Valdralis queda en silencio.",
        )
        await send_admin(f"Error generando escena: {type(exc).__name__}: {exc}")
        return

    reply = str(scene["reply"]).strip()
    data = load_data()
    previous_state = data.get("state") or default_state()
    try:
        current_chapter_number = int(previous_state.get("current_chapter_number") or 0) or None
    except (TypeError, ValueError):
        current_chapter_number = None
    scene_state = narrative_state_update(scene.get("state"), current_chapter_number)
    data["state"] = merge_state(previous_state, scene_state)
    transition = scene.get("chapter_transition")
    completed_chapter = completed_chapter_from_transition(data["state"], transition)
    transition_requested = isinstance(transition, dict) and bool(transition.get("completed"))
    if transition_requested and completed_chapter is None:
        await send_admin(
            "La IA intento una transicion de capitulo invalida o un salto de numeracion. "
            "El sistema la ha rechazado y mantiene el capitulo actual."
        )
        transition = {"completed": False}
    general_beats_ready = completed_chapter and chapter_ready_by_scene_progress(data["state"], completed_chapter)
    required_events_ready = completed_chapter and chapter_required_events_ready(data["state"], completed_chapter)
    if completed_chapter and not (general_beats_ready and required_events_ready):
        progress = chapter_scene_progress_markdown(data["state"].get("chapter_scene_progress"), completed_chapter)
        required_events = required_event_progress_markdown(
            data["state"].get("required_event_progress"),
            completed_chapter,
        )
        await send_admin(
            "La IA intento cerrar un capitulo sin completar sus escenas o hitos obligatorios. "
            "No he cerrado el capitulo.\n\n"
            f"{progress}\n\n{required_events}"
        )
        completed_chapter = None
        transition = {"completed": False}
    if completed_chapter:
        title = CHAPTER_TITLES.get(completed_chapter, f"Capítulo {completed_chapter}")
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
        save_chapter_summary(
            completed_chapter,
            title,
            summary,
            state_snapshot=data["state"],
        )
        merged_story_state = data["state"]
        data = load_data()
        data["state"] = merged_story_state
        await send_admin(f"Resumen canonico guardado para {chapter_label(completed_chapter)}:\n{summary}")

    chapter_banner = apply_chapter_transition(data, transition)
    if chapter_banner:
        reply = f"{reply}\n\n---\n\n{chapter_banner}"
    if completed_chapter and not (data.get("state") or {}).get("season_complete"):
        pause_until = activate_chapter_review_pause(data, completed_chapter)
        if pause_until:
            reply = (
                f"{reply}\n\n"
                f"{chapter_review_pause_reply(data)}"
            )
            await send_admin(
                f"Pausa de revision activada tras {chapter_label(completed_chapter)}.\n"
                f"- Hasta: {pause_until}\n"
                "- Usa /capitulos para revisar resumenes, /memoria para ver estado, "
                "/corregir_memoria para ajustar canon o /reanudar para abrir el siguiente capitulo."
            )
    try:
        await send_telegram_text(
            narrador_app.bot,
            chat_id=chat_id,
            text=reply,
        )
    except TelegramTextDeliveryError as exc:
        pending_data = load_data()
        pending_data["pending_narrator_delivery"] = {
            "text": reply,
            "chapter_number": turn_chapter_number,
            "sent_chunks": exc.sent_chunks,
            "total_chunks": exc.total_chunks,
            "state_after_delivery": data.get("state"),
            "chapter_review_pause_after_delivery": data.get("chapter_review_pause"),
            "created_at": now_iso(),
            "last_error": f"{type(exc.cause).__name__}: {exc.cause}",
        }
        save_data(pending_data)
        try:
            await send_admin(
                "La respuesta narrativa se genero, pero Telegram no confirmo su entrega a Sandra "
                f"tras tres intentos. Fragmentos confirmados: {exc.sent_chunks}/{exc.total_chunks}.\n"
                "La respuesta queda pendiente; usa /reenviar_ultimo para completar el envio sin llamar otra vez a la IA."
            )
        except Exception:
            logger.exception("Tampoco se pudo avisar al chat de control del fallo de entrega")
        return

    data["pending_narrator_delivery"] = None
    save_data(data)
    append_history("Narrador", reply, chapter_number=turn_chapter_number)

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
        await update.effective_chat.send_message(
            "El reloj sin minutos ha detenido sus agujas. Valdralis guarda silencio."
        )
        return

    if chapter_review_pause_is_active(data):
        await update.effective_chat.send_message(chapter_review_pause_reply(data))
        await send_admin(
            "Sandra ha escrito durante un cierre de revision de capitulo. "
            "He enviado el aviso fijo de hito alcanzado; no he llamado a la IA ni he guardado el mensaje en la memoria narrativa.\n\n"
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
        f"- Respuesta pendiente de entrega: {'si' if isinstance(data.get('pending_narrator_delivery'), dict) else 'no'}",
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
        if review_pause.get("requires_manual_resume"):
            lines.append("- Revision: cerrada hasta que Miguel use /reanudar")
        else:
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


async def cmd_exportar_capitulo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_admin(update):
        return
    data = load_data()
    if context.args:
        if not context.args[0].isdigit():
            await update.effective_chat.send_message("Uso: /exportar_capitulo 1")
            return
        chapter_number = int(context.args[0])
    else:
        try:
            chapter_number = int((data.get("state") or {}).get("current_chapter_number") or 0)
        except (TypeError, ValueError):
            chapter_number = 0
    if chapter_number < 1 or chapter_number > FINAL_CHAPTER_NUMBER:
        await update.effective_chat.send_message(
            f"Indica un capítulo entre 1 y {FINAL_CHAPTER_NUMBER}."
        )
        return

    try:
        content, message_count, closed = build_chapter_export(chapter_number)
    except Exception as exc:
        logger.exception("No se pudo exportar el capitulo %s", chapter_number)
        await update.effective_chat.send_message(
            f"No pude crear la exportación: {type(exc).__name__}: {exc}"
        )
        return
    if message_count == 0:
        await update.effective_chat.send_message(
            f"Todavía no hay mensajes guardados para {chapter_label(chapter_number)}."
        )
        return

    filename = f"academia_valdralis_capitulo_{chapter_number:02d}.md"
    document = io.BytesIO(content.encode("utf-8"))
    document.name = filename
    status = "cerrado y con resumen canónico" if closed else "provisional, todavía sin cierre canónico"
    await update.effective_chat.send_document(
        document=document,
        filename=filename,
        caption=(
            f"{chapter_label(chapter_number)} exportado: {message_count} mensajes. "
            f"Estado: {status}."
        ),
    )


async def cmd_personajes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_admin(update):
        return
    state = load_data().get("state") or default_state()
    text = "# Fichas vivas de personajes\n\n" + character_sheets_markdown(
        state.get("character_sheets") or {}
    )
    for chunk in split_long(text):
        await update.effective_chat.send_message(chunk)


async def cmd_progreso(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_admin(update):
        return
    state = load_data().get("state") or default_state()
    chapter_number: int | None = None
    if context.args and context.args[0].isdigit():
        chapter_number = max(1, min(FINAL_CHAPTER_NUMBER, int(context.args[0])))
    else:
        try:
            chapter_number = int(state.get("current_chapter_number") or 0) or None
        except (TypeError, ValueError):
            chapter_number = None
    text = "# Progreso de escenas\n\n" + chapter_scene_progress_markdown(
        state.get("chapter_scene_progress") or {},
        chapter_number,
    )
    if chapter_number in CHAPTER_REQUIRED_EVENTS:
        text += "\n\n# Hitos obligatorios\n\n" + required_event_progress_markdown(
            state.get("required_event_progress") or {},
            chapter_number,
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


async def cmd_reenviar_ultimo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_admin(update):
        return
    data = load_data()
    if not narrador_app or not data.get("sandra_chat_id"):
        await update.effective_chat.send_message(
            "No puedo reenviar: el narrador no esta disponible o Sandra no esta vinculada."
        )
        return

    if isinstance(data.get("pending_narrator_delivery"), dict):
        pending = data["pending_narrator_delivery"]
        data["last_narrator_resend_attempt"] = {
            "status": "started",
            "source": "pending",
            "started_at": now_iso(),
            "sent_chunks_before": int(pending.get("sent_chunks") or 0),
            "total_chunks": int(pending.get("total_chunks") or len(split_long(str(pending.get("text") or "")))),
        }
        save_data(data)
        delivered = await deliver_pending_narrator_reply()
        data = load_data()
        attempt = data.get("last_narrator_resend_attempt")
        if isinstance(attempt, dict):
            attempt["status"] = "confirmed" if delivered else "failed"
            attempt["finished_at"] = now_iso()
            data["last_narrator_resend_attempt"] = attempt
            if delivered:
                data["last_narrator_resend"] = {
                    "chapter_number": pending.get("chapter_number"),
                    "chunks_sent": int(pending.get("total_chunks") or len(split_long(str(pending.get("text") or "")))),
                    "sent_at": now_iso(),
                }
            save_data(data)
        await update.effective_chat.send_message(
            "Respuesta pendiente entregada a Sandra y continuidad confirmada."
            if delivered
            else "Telegram sigue sin confirmar la entrega. La respuesta permanece pendiente."
        )
        return

    latest = latest_narrator_message()
    if not latest or not latest[0].strip():
        await update.effective_chat.send_message("No encuentro una respuesta del narrador para reenviar.")
        return
    text, chapter_number = latest
    data["last_narrator_resend_attempt"] = {
        "status": "started",
        "source": "stored_history",
        "chapter_number": chapter_number,
        "started_at": now_iso(),
        "total_chunks": len(split_long(text)),
    }
    save_data(data)
    try:
        chunks_sent = await send_telegram_text(
            narrador_app.bot,
            chat_id=int(data["sandra_chat_id"]),
            text=text,
        )
    except TelegramTextDeliveryError as exc:
        data = load_data()
        attempt = data.get("last_narrator_resend_attempt")
        if isinstance(attempt, dict):
            attempt.update(
                {
                    "status": "failed",
                    "finished_at": now_iso(),
                    "sent_chunks": exc.sent_chunks,
                    "total_chunks": exc.total_chunks,
                    "error_type": type(exc.cause).__name__,
                }
            )
            data["last_narrator_resend_attempt"] = attempt
            save_data(data)
        await update.effective_chat.send_message(
            "Telegram no ha confirmado el reenvio. "
            f"Fragmentos confirmados: {exc.sent_chunks}/{exc.total_chunks}."
        )
        return

    data = load_data()
    data["last_narrator_resend"] = {
        "chapter_number": chapter_number,
        "chunks_sent": chunks_sent,
        "sent_at": now_iso(),
    }
    attempt = data.get("last_narrator_resend_attempt")
    if isinstance(attempt, dict):
        attempt.update(
            {
                "status": "confirmed",
                "finished_at": now_iso(),
                "sent_chunks": chunks_sent,
            }
        )
        data["last_narrator_resend_attempt"] = attempt
    save_data(data)
    await update.effective_chat.send_message(
        f"Ultima respuesta del narrador reenviada a Sandra en {chunks_sent} mensaje/s. No he llamado a la IA."
    )


async def cmd_entrega(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_admin(update):
        return
    data = load_data()
    pending = data.get("pending_narrator_delivery")
    attempt = data.get("last_narrator_resend_attempt")
    confirmed = data.get("last_narrator_resend")
    control_error = data.get("last_control_error")
    lines = ["Estado de entrega del narrador"]

    if isinstance(pending, dict):
        lines.append(
            "- Respuesta pendiente: sí "
            f"({int(pending.get('sent_chunks') or 0)}/{int(pending.get('total_chunks') or 0)} mensajes confirmados)"
        )
    else:
        lines.append("- Respuesta pendiente: no")

    if isinstance(attempt, dict):
        labels = {
            "started": "iniciado, sin confirmación final",
            "confirmed": "confirmado por Telegram",
            "failed": "fallido",
        }
        status = labels.get(str(attempt.get("status") or ""), "desconocido")
        lines.append(f"- Último intento: {status}")
        if attempt.get("finished_at") or attempt.get("started_at"):
            lines.append(f"- Momento del intento: {attempt.get('finished_at') or attempt.get('started_at')}")
        if attempt.get("sent_chunks") is not None:
            lines.append(
                f"- Mensajes confirmados en el intento: {attempt.get('sent_chunks')}/{attempt.get('total_chunks', '?')}"
            )
    else:
        lines.append("- Último intento registrado: ninguno")

    if isinstance(confirmed, dict):
        lines.append(f"- Último reenvío confirmado: {confirmed.get('sent_at', 'sin fecha')}")
        lines.append(f"- Partes entregadas: {confirmed.get('chunks_sent', '?')}")
    else:
        lines.append("- Reenvío confirmado: no consta")

    if isinstance(control_error, dict):
        lines.append(
            f"- Último error del control: {control_error.get('error_type', 'desconocido')} "
            f"({control_error.get('at', 'sin fecha')})"
        )

    lines.append("- Lectura por Sandra: Telegram no ofrece confirmación de lectura a los bots")
    await update.effective_chat.send_message("\n".join(lines))


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


async def send_chapter_opening(chapter_number: int) -> bool:
    if not narrador_app:
        return False
    data = load_data()
    sandra_id = data.get("sandra_chat_id")
    if not sandra_id:
        return False
    sent_openings = {
        int(number)
        for number in (data.get("sent_chapter_openings") or [])
        if str(number).isdigit()
    }
    if chapter_number in sent_openings:
        return False
    message = read_chapter_opening(chapter_number)
    if not message:
        return False
    if len(message) > 3900:
        raise RuntimeError("La apertura de capitulo debe caber en un solo mensaje de Telegram")

    await narrador_app.bot.send_message(chat_id=int(sandra_id), text=message)
    try:
        append_history("Narrador", message, chapter_number=chapter_number)
        data = load_data()
        sent_openings = {
            int(number)
            for number in (data.get("sent_chapter_openings") or [])
            if str(number).isdigit()
        }
        sent_openings.add(chapter_number)
        data["sent_chapter_openings"] = sorted(sent_openings)
        save_data(data)
    except Exception:
        logger.exception(
            "La apertura del capitulo %s se envio, pero no pudo registrarse en memoria",
            chapter_number,
        )
    return True


async def cmd_reanudar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not is_admin(update):
        return
    data = load_data()
    original_data = copy.deepcopy(data)
    data["paused"] = False
    opened_chapter = open_pending_chapter_after_review(data)
    if opened_chapter:
        opening = read_chapter_opening(opened_chapter)
        sent_openings = {
            int(number)
            for number in (data.get("sent_chapter_openings") or [])
            if str(number).isdigit()
        }
        if (
            not narrador_app
            or not data.get("sandra_chat_id")
            or not opening
            or len(opening) > 3900
            or opened_chapter in sent_openings
        ):
            await update.effective_chat.send_message(
                "No he reanudado la partida: no puedo garantizar el envio unico de la apertura a Sandra."
            )
            return
    data["chapter_review_pause"] = None
    save_data(data)
    if opened_chapter:
        try:
            opening_sent = await send_chapter_opening(opened_chapter)
        except Exception as exc:
            logger.exception("No se pudo enviar la apertura del capitulo")
            save_data(original_data)
            await update.effective_chat.send_message(
                f"No he reanudado la partida porque la apertura no pudo enviarse: {type(exc).__name__}."
            )
            return
        if not opening_sent:
            save_data(original_data)
            await update.effective_chat.send_message(
                "No he reanudado la partida porque la apertura no pudo confirmarse."
            )
            return
        await update.effective_chat.send_message(
            f"Partida reanudada. {chapter_label(opened_chapter)} queda abierto y su apertura se ha enviado a Sandra."
        )
    else:
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
    append_history("Preludio", message, chapter_number=0)
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
    append_history("Narrador", message, chapter_number=1)
    data = load_data()
    data["story_start_sent"] = True
    data["state"] = {
        **(data.get("state") or default_state()),
        "chapter": chapter_label(1),
        "current_chapter_number": 1,
        "completed_chapters": [],
        "season_complete": False,
        "location": "Casa de Dario",
        "current_scene": "Sandra esta encerrada en su habitacion tras discutir con Dario y acaba de oir la carta entrar por debajo de la puerta principal",
        "next_suggested_scene": "Sandra decide si intenta salir, escucha la carta, busca otra salida o espera a que Dario se aleje",
        "character_sheets": default_character_sheets(),
        "chapter_scene_progress": default_chapter_scene_progress(),
        "required_event_progress": default_required_event_progress(),
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


async def control_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    logger.error(
        "Error no controlado en el bot de control",
        exc_info=(type(error), error, error.__traceback__) if error else None,
    )
    try:
        data = load_data()
        data["last_control_error"] = {
            "at": now_iso(),
            "error_type": type(error).__name__ if error else "UnknownError",
        }
        save_data(data)
    except Exception:
        logger.exception("No se pudo registrar el error del bot de control")

    if not isinstance(update, Update) or not update.effective_chat or not is_admin(update):
        return
    try:
        await update.effective_chat.send_message(
            "El comando ha fallado antes de poder confirmar su resultado. "
            "No repitas un reenvío a ciegas; usa /entrega para consultar el registro."
        )
    except Exception:
        logger.exception("No se pudo informar del error en el chat de control")


def build_control_app() -> Application:
    app = ApplicationBuilder().token(require_env("TOKEN_CONTROL")).build()
    app.add_handler(CommandHandler("start", control_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("estado", cmd_estado))
    app.add_handler(CommandHandler("memoria", cmd_memoria))
    app.add_handler(CommandHandler("capitulos", cmd_capitulos))
    app.add_handler(CommandHandler("exportar_capitulo", cmd_exportar_capitulo))
    app.add_handler(CommandHandler("personajes", cmd_personajes))
    app.add_handler(CommandHandler("progreso", cmd_progreso))
    app.add_handler(CommandHandler("historial", cmd_historial))
    app.add_handler(CommandHandler("entrega", cmd_entrega))
    app.add_handler(CommandHandler("reenviar_ultimo", cmd_reenviar_ultimo))
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
    app.add_error_handler(control_error_handler)
    return app


def build_narrador_app() -> Application:
    app = ApplicationBuilder().token(require_env("TOKEN_NARRADOR")).build()
    app.add_handler(CommandHandler("start", narrador_start))
    app.add_handler(CommandHandler("ayuda", narrador_help))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_narrador_command))
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
