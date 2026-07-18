# nuebot — Telegram ↔ Stable Diffusion WebUI (A1111 / Forge)

Bot minimal: cualquier mensaje de texto (≥3 palabras) → prompt directo a `txt2img`.
El resultado llega con botones **Repetir** y **HR Upscale**. El HR Upscale llega con
**Repetir** y **Upscale Final**. Cancelable por task_id. Sin DB, sin presets,
sin prefs — el alcance pedido.

## Stack

| Capa         | Elección                       | Por qué                                                                |
| ------------ | ------------------------------ | ---------------------------------------------------------------------- |
| Bot          | **aiogram 3.29.1**             | Estándar async en Python, API 7.x, FSM, callbacks bien soportados       |
| HTTP SD      | **httpx 0.28 (async)**         | Timeouts claros, HTTP/2, maneja mejor tunnels Gradio que aiohttp       |
| Config       | **pydantic-settings 2.14**     | `.env` tipado, validación en arranque                                  |
| Cola         | **asyncio.Queue + worker**     | Un job en GPU a la vez. FIFO. Cancel por task_id                       |
| Cache result | `OrderedDict(maxsize=256)`     | Suficiente para uso personal; state ephemeral                          |

## Layout

```
nuebot/
├── .env.example
├── pyproject.toml
├── start_bot.py            # entrypoint: `python start_bot.py`
├── data/
│   └── outputs/            # PNG local por entrega (debug)
└── src/
    └── nuebot/
        ├── __init__.py
        ├── config.py       # Settings (pydantic-settings) — SD_API_URL, BOT_TOKEN
        ├── bot.py          # Dispatcher, registra routers, arranca worker
        ├── sd/
        │   ├── __init__.py
        │   └── client.py   # txt2img / extra-single-image / interrupt / options
        ├── jobs/
        │   ├── __init__.py
        │   └── manager.py  # Queue FIFO + current_job + cancel + result_cache
        └── handlers/
            ├── __init__.py
            ├── generate.py # texto libre → encola Job
            ├── cancel.py   # /cancel <id>
            └── buttons.py  # callback_data: rep: / up: / upx:
```

`__init__.py` en cada paquete para que `python -m nuebot.bot` ande limpio.

## Flujo

### 1) Usuario manda texto

```
handler generate.py:
  text = message.text
  if text.startswith("/") → router de comandos, skip
  if len(text.split()) < 3 → "Mandame un prompt de al menos 3 palabras."
  prompt = text
  job = Job(task_id=uuid4().hex[:8], prompt=prompt, chat_id=..., message_id=...)
  await queue.put(job)
  await message.reply(f"🟡 Cola #{pos} · task {task_id}")
```

### 2) Worker procesa

```
worker loop:
  job = await queue.get()
  current_job = job
  reply al usuario: "🎨 Generando #1 · task <id> ..."
  try:
      b64_list = await sd.txt2img(prompt=job.prompt, seed=-1, ...)
      img_b64 = b64_list[0]
      result_cache[job.task_id] = JobParams(prompt=..., seed=...)
      photo = BufferedInputFile(b64decode(img_b64), f"{task_id}_txt2img.png")
      await bot.send_photo(chat_id, photo,
          caption=f"✅ {prompt[:200]}\ntask: `{task_id}`",
          reply_markup=kb_txt2img(task_id))   # [Repetir] [HR Upscale]
  except CancelledError:
      await bot.send_message(chat_id, "❌ Cancelado.")
  finally:
      current_job = None
      queue.task_done()
```

### 3) Botones (callback_data ≤ 64 bytes)

- `rep:<task_id>` → re-ejecuta txt2img con los params cacheados → botones Repetir / HR Upscale
- `up:<task_id>` → `enable_hr=True` + receta hardcoded → botones Repetir / Upscale Final
- `upx:<task_id>` → `POST /extra-single-image` con mismo upscaler x3 → sin más botones

Receta HR hardcoded (sin presets por ahora):

```python
payload["enable_hr"] = True
payload["hr_scale"] = 1.5
payload["hr_upscaler"] = settings.hr_upscaler        # .env
payload["hr_second_pass_steps"] = 4
payload["denoising_strength"] = 0.3
payload["hr_resize_mode"] = "lanczos"
```

Receta Upscale Final hardcoded:

```python
{"image": b64, "upscaler_1": settings.hr_upscaler,
 "upscaling_resize": settings.final_upscale_factor,  # default 3.0
 "resize_mode": 0, "show_extras_results": False}
```

### 4) Cancel

```
handler cancel.py:
  /cancel <task_id>
    if task_id == current_job.task_id → sd.interrupt() + future.cancel
    elif en pending → remover + cancelar future
    else → "no encontrado, ya terminó"
```

## Endpoints A1111/Forge

| Acción                 | Método + ruta                              | Notas                          |
| ---------------------- | ------------------------------------------ | ------------------------------ |
| Generar                | `POST /sdapi/v1/txt2img`                  | `data["images"]` lista de b64  |
| HR upscale             | mismo endpoint con `enable_hr=True`        | una sola llamada               |
| Final upscale          | `POST /sdapi/v1/extra-single-image`        | `data["image"]` único          |
| Cancelar job actual    | `POST /sdapi/v1/interrupt`                | sin body                       |
| Health check           | `GET /sdapi/v1/options`                   |                                |

## .env (única fuente de verdad)

```env
BOT_TOKEN=        # de @BotFather
SD_API_URL=http://127.0.0.1:7860
SD_TIMEOUT_S=600  # cold start 30-90s, dale margen
HR_UPSCALER=4x_NMKD-Superscale-SP_178000_G
FINAL_UPSCALE_FACTOR=3.0
ALLOWED_USER_ID=  # opcional, blanco = cualquiera
```

## Verificación (cuando el endpoint SD esté vivo)

1. `python start_bot.py` levanta
2. `GET /sdapi/v1/options` desde `httpx.AsyncClient` → 200
3. `bot.get_me()` → username + id
4. Mandar un prompt de prueba → recibe imagen con botones Repetir / HR Upscale
5. Pulsar HR Upscale → llega imagen más grande con Repetir / Upscale Final
6. Pulsar Upscale Final → llega imagen más grande sin botones

## Lo que NO se hace (YAGNI explícito)

- DB / SQLite / persistencia entre reinicios (estado en RAM alcanza para uso personal)
- Presets por modelo (recomendación: si vas a usar varios modelos, agregar `presets.py` con `Dict[alias, Preset]`; verificar `len(PRESETS) == len(*_PRESET)` en import)
- Prefs por usuario (default 512x512, sampler Euler, steps 20, cfg 7 — sano para el 80% de modelos)
- n_iter / batch_size expuesto al usuario (cap interno 1 imagen por job)
- `/gen`, `/imagine`, `/dream`, etc. (texto libre ya cubre todo)
- Sistema de presets negativo (un negative genérico hardcoded: "lowres, bad anatomy, bad hands, text, error, missing fingers")
- Panel admin, métricas, logs estructurados, Sentry
- ConversationHandler para wizards (no hay wizard: prompt es directo)

## Pitfalls ya conocidos (del skill `telegram-sd-bot`)

- **Shadowing `queue/`**: el paquete se llama `jobs/`, no `queue/`.
- **`POST /options` retorna body vacío en Forge/Gradio**: re-GET para confirmar.
- **`extra-single-image` devuelve `data["image"]` (singular), no `data["images"]`**.
- **Cold start SD**: `SD_TIMEOUT_S=600`, no 30.
- **`callback_data` ≤ 64 bytes**: `task_id = uuid4().hex[:8]` (8 chars), prefijo `rep:`/`up:`/`upx:` (3-4 chars). Queda < 20 bytes.
- **parse_mode Markdown + prompt del usuario**: caption sin parse_mode para evitar crash por `*`/`_`/backticks en prompts.
- **Bug de presets no registrados**: si alguna vez se agregan presets, este bot no los tiene. Documentado acá, no aplica ahora.
