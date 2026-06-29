# Control Partida Sandra

Bot de Telegram para dirigir una novela interactiva de rol en la Academia de Valdralis.

El proyecto usa dos bots:

- **Academia de Valdralis**: bot narrador que habla con Sandra.
- **Control Partida Sandra**: bot privado de Miguel para estado, notas, historial y resumenes.

## Configuracion local

1. Crea un archivo `.env` a partir de `.env.example`.
2. Rellena `TOKEN_NARRADOR`, `TOKEN_CONTROL`, `MI_CHAT_ID` y `OPENAI_API_KEY`.
3. `SANDRA_CHAT_ID` puede quedarse vacio: se captura cuando Sandra escribe `/start` al narrador.

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

La partida guarda memoria de dos formas:

- `data/data.json`: estado estructurado usado por la IA.
- `data/memoria_actual.md`: resumen legible para revisar como humano.

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
```

Para persistencia simple en Railway, crea un Volume y monta `/app/data`.
