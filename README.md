# nuebot — Telegram ↔ Stable Diffusion

Bot minimal. Mandás texto al bot → genera un PNG con A1111 / Forge y lo envía
como **documento**, sin recompresión. Cada documento lleva el caption de
metadata del screenshot y botones **✨ UPSCALE** / **🔄 REPETIR**.

## Setup

```bash
# 1) editar .env (copiá .env.example primero)
cp .env.example .env
#    - BOT_TOKEN de @BotFather
#    - SD_API_URL del WebUI (default http://127.0.0.1:7860)

# 2) correr
uv run python start_bot.py
```

## Uso

| Lo que mandás              | Qué pasa                                          |
| -------------------------- | ------------------------------------------------- |
| `un astronauta montando un caballo` | txt2img → botones Repetir / HR Upscale |
| `/status`                  | muestra el job corriendo y la cola                |
| `/cancel a1b2c3d4`         | corta ese job (o lo saca de la cola)              |

Botones sobre cada documento:
- **🔄 REPETIR** — vuelve a generar usando el snapshot exacto guardado.
- **✨ UPSCALE** — re-genera con `enable_hr=true` (upscaler del `.env`, x1.5).
- **✨ UPSCALE FINAL** — aplica `/extra-single-image` x3 al resultado HR.

Los parámetros completos de cada job se guardan en `data/jobs/<task_id>.json`.
Esto permite que Repetir y Upscale sigan funcionando después de reiniciar el bot.

## Endpoints SD que usa

| Acción             | Método + ruta                              |
| ------------------ | ------------------------------------------ |
| Generar / HR       | `POST /sdapi/v1/txt2img`                   |
| Upscale Final      | `POST /sdapi/v1/extra-single-image`        |
| Cancelar           | `POST /sdapi/v1/interrupt`                 |
| Health (arranque)  | `GET /sdapi/v1/options`                    |

## Lo que NO hace (a propósito)

Sin DB, sin persistencia entre reinicios, sin presets por modelo, sin panel
admin, sin `/imagine`, sin wizards. Para uso personal eso es exactamente lo
que necesitás; cuando agregues un segundo modelo o un segundo usuario, el
plan en `.hermes/PLAN.md` tiene la lista de cosas a sumar.
