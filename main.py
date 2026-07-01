import asyncio
import json
import logging
import os
import signal
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from openai import AsyncOpenAI
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


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Falta la variable {name}")
    return value


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        ],
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
    }


def load_data() -> dict[str, Any]:
    if not DATA_FILE.exists():
        return default_data()
    try:
        loaded = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.exception("data.json no es JSON valido; usando estado por defecto")
        return default_data()

    data = default_data()
    data.update(loaded)
    if not data.get("state"):
        data["state"] = default_state()
    return data


def save_data(data: dict[str, Any]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    write_memory_markdown(data)


def markdown_list(items: list[Any]) -> str:
    clean_items = [str(item).strip() for item in items if str(item).strip()]
    if not clean_items:
        return "- Pendiente"
    return "\n".join(f"- {item}" for item in clean_items)


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

## Objetos relevantes

{markdown_list(state.get('inventory') or [])}

## Hilos abiertos

{markdown_list(state.get('open_threads') or [])}

## Secretos revelados

{markdown_list(state.get('revealed_secrets') or [])}

## Secretos que la IA debe recordar pero no revelar antes de tiempo

{markdown_list(state.get('unrevealed_secrets_reminder') or [])}

## Notas recientes de Miguel

{markdown_list(note_lines)}

## Historial reciente

{markdown_list(history_lines)}
"""
    MEMORY_MD_PATH.parent.mkdir(parents=True, exist_ok=True)
    MEMORY_MD_PATH.write_text(content, encoding="utf-8")


def append_history(role: str, text: str) -> None:
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
    history = load_data().get("history", [])[-limit:]
    if not history:
        return "No hay historial previo."
    lines = []
    for item in history:
        role = item.get("role", "desconocido")
        text = str(item.get("text", "")).strip()
        lines.append(f"{role}: {text}")
    return "\n".join(lines)


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
- Cuando termine un capitulo, marca chapter_transition.completed=true, pero no escribas tu el cartel de "Capitulo terminado"; el sistema lo anadira.
- Tras completar el capitulo 10, marca season_complete=true y no abras un capitulo 11.

BIBLIA DE LA PARTIDA:
{read_lore()}

ESTADO ACTUAL:
{state_text()}

NOTAS RECIENTES DE MIGUEL:
{admin_notes_text()}

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
  "admin_note": "nota breve para Miguel solo si hay duda importante de lore o direccion; si no, cadena vacia"
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
    data["state"] = {**previous_state, **scene_state}
    chapter_banner = apply_chapter_transition(data, scene.get("chapter_transition"))
    if chapter_banner:
        reply = f"{reply}\n\n---\n\n{chapter_banner}"
    save_data(data)
    append_history("Narrador", reply)

    for chunk in split_long(reply):
        await narrador_app.bot.send_message(chat_id=chat_id, text=chunk)

    admin_note = str(scene.get("admin_note") or "").strip()
    if admin_note:
        await send_admin(f"Nota de direccion:\n{admin_note}")


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
    state = data.get("state") or {}
    lines = [
        "Estado Control Partida Sandra",
        f"- Narrador vinculado: {'si' if data.get('sandra_chat_id') else 'no'}",
        f"- Pausado: {'si' if data.get('paused') else 'no'}",
        f"- OpenAI: {'configurado' if openai_available() else 'pendiente'}",
        f"- Modelo: {OPENAI_MODEL}",
        f"- Capitulo: {state.get('chapter', 'Pendiente')}",
        f"- Primer curso terminado: {'si' if state.get('season_complete') else 'no'}",
        f"- Mensajes guardados: {len(data.get('history', []))}",
        f"- Antesala activa: {'si' if prelude_guard_active() else 'no'}",
        f"- Inicio de partida: {STORY_START_DATE.isoformat()} {STORY_START_HOUR:02d}:{STORY_START_MINUTE:02d}",
        f"- Resumen diario: {DAILY_SUMMARY_HOUR:02d}:{DAILY_SUMMARY_MINUTE:02d}",
    ]
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
        "current_scene": "Sandra acaba de recibir la carta de Valdralis",
        "next_suggested_scene": "Sandra decide si abre la carta, la esconde o escucha a su padre",
    }
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
