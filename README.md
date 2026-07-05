# Control Partida Sandra

Bot de Telegram para dirigir una novela interactiva de rol en la Academia de Valdralis.

El proyecto usa dos bots:

- **Academia de Valdralis**: bot narrador que habla con Sandra.
- **Control Partida Sandra**: bot privado de Miguel para estado, notas, historial y resumenes.

## Configuracion local

1. Crea un archivo `.env` a partir de `.env.example`.
2. Rellena `TOKEN_NARRADOR`, `TOKEN_CONTROL`, `MI_CHAT_ID` y `OPENAI_API_KEY`.
3. `SANDRA_CHAT_ID` puede quedarse vacio: se captura cuando Sandra escribe `/start` al narrador.
4. En Railway, usa Postgres y configura `DATABASE_URL`. Es la memoria durable de la partida.

Instalacion:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

## Comandos de control

En el bot privado:

```text
/status
/estado
/memoria
/capitulos
/personajes
/historial 20
/resumen
/probar texto de Sandra para hacer una prueba privada
/preludio_status
/preludio_preview YYYY-MM-DD
/preludio_on
/preludio_off
/preludio_enviar_hoy YYYY-MM-DD
/inicio_preview
/inicio_enviar
/nota texto para orientar a la IA
/corregir_memoria texto con la correccion canonica
/decir texto manual para Sandra
/pausar
/reanudar
```

## Flujo

Sandra escribe al narrador como si escribiera una novela. El bot:

1. Lee `lore/biblia.md`.
2. Lee la memoria actual.
3. Lee los ultimos mensajes.
4. Responde en estilo literario.
5. Actualiza la memoria interna.
6. Avisa a Miguel si hay una decision de lore importante.

`/probar` solo responde al bot de control. No envia nada a Sandra y no guarda memoria.

Los mensajes de Sandra se agrupan durante `MESSAGE_BUFFER_SECONDS` segundos. Si Sandra manda varias frases seguidas, el bot espera 25 segundos desde el ultimo mensaje y responde a todo junto.

La partida guarda memoria de varias formas:

- Postgres `app_state`: estado estructurado durable usado por la IA.
- Postgres `story_messages`: log completo de mensajes y respuestas.
- Postgres `chapter_summaries`: resumen canonico de cada capitulo cerrado.
- `data/memoria_actual.md`: resumen legible para revisar como humano.

`data/data.json` queda como copia espejo/respaldo local. En Railway, la memoria importante debe estar en Postgres.

El estado incluye fichas vivas de personajes (`character_sheets`) para Lucien, Kael, Aurelian, Kilnip, Nora y otros personajes recurrentes. Cada ficha guarda relacion con Sandra, secretos que sabe, ultima escena juntos, tension romantica y cosas que no debe revelar todavia. Se pueden revisar desde el bot de control con `/personajes` o dentro de `/memoria`.

La biblia de lore incluye tambien un resumen de trama de temporada, un grimorio practico de hechizos y un bestiario inicial. Estas secciones sirven para que la IA tenga recursos concretos de clases, criaturas, amenazas y soluciones sin improvisar siempre lo mismo.

## Capitulos

La IA mantiene el capitulo actual en memoria. Cuando se cumple el objetivo dramatico de un capitulo, el sistema anade automaticamente:

```text
Capitulo X terminado.

Capitulo Y: Titulo
```

Al terminar el capitulo 10, el primer curso queda cerrado y el bot no continua la historia hasta el curso siguiente.

Al cerrar cada capitulo, el bot genera y guarda un resumen canonico. La IA recibe esos resumenes antes de responder, para mantener continuidad aunque pasen meses.

Despues de cada capitulo cerrado, el bot puede activar una pausa de revision. Por defecto son 14 dias (`CHAPTER_REVIEW_PAUSE_DAYS=14`). Durante esa pausa, Sandra recibe una respuesta narrativa de interludio y la IA no continua la historia. Miguel puede revisar `/capitulos`, `/memoria`, usar `/corregir_memoria` y reabrir antes con `/reanudar`.

## Preludio

Los mensajes previos al cumpleanos viven en `lore/preludio.md`. El mensaje que abre la partida vive en `lore/inicio.md`.

Por defecto no se envian hasta activar:

```text
/preludio_on
```

Para revisar antes de enviar:

```text
/preludio_status
/preludio_preview 2026-06-29
/inicio_preview
```

El rango recomendado es del 29 de junio de 2026 al 12 de julio de 2026. La partida empieza con `lore/inicio.md` el 13 de julio de 2026 a las 00:01.

Hasta `STORY_START_DATE` a la hora configurada, si Sandra contesta al bot narrador, el bot responde con instrucciones fijas, avisa a Miguel y no llama a la IA, no inicia la partida y no guarda memoria.

## Railway

Variables necesarias:

```env
TOKEN_NARRADOR=...
TOKEN_CONTROL=...
MI_CHAT_ID=...
SANDRA_CHAT_ID=
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-5.5
DATABASE_URL=postgresql://...
DATA_FILE=/app/data/data.json
MEMORY_MD_PATH=/app/data/memoria_actual.md
APP_TIMEZONE=Europe/Madrid
DAILY_SUMMARY_HOUR=23
DAILY_SUMMARY_MINUTE=0
PRELUDE_ENABLED=false
PRELUDE_PATH=lore/preludio.md
START_MESSAGE_PATH=lore/inicio.md
PRELUDE_START_DATE=2026-06-29
PRELUDE_END_DATE=2026-07-12
STORY_START_DATE=2026-07-13
PRELUDE_REPLY_ENABLED=true
PRELUDE_HOUR=21
PRELUDE_MINUTE=30
STORY_START_HOUR=0
STORY_START_MINUTE=1
MESSAGE_BUFFER_SECONDS=25
CHAPTER_REVIEW_PAUSE_DAYS=14
```

Para persistencia simple en Railway, crea un Volume y monta `/app/data`.

Para la partida real, crea tambien un plugin Postgres en Railway. El bot creara automaticamente:

- `app_state`
- `story_messages`
- `chapter_summaries`
