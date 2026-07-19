"""Bot entrypoint.

Levanta el Dispatcher, registra routers, abre el SDClient, arranca el worker
de la cola, hace polling. Ctrl+C lo baja limpio.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from .config import get_settings, load_generation_settings
from .handlers import buttons, cancel, generate
from .handlers.buttons import Retry
from .jobs.manager import Job, JobManager, apply_result_info, new_task_id
from .sd.client import SDClient

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "outputs"
DATA_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("nuebot")


async def _handle_job(job: Job, bot: Bot, sd: SDClient, jobs: JobManager) -> None:
    params = job.params
    assert params is not None, "enqueue garantiza params"

    if getattr(job, "_cancelled", False):
        return

    # Sin mensaje "Generando..." en el chat; lo borra el manager al terminar.
    # El único mensaje que queda en el chat será el resultado final.
    status = await bot.send_message(job.chat_id, f"🎨 Generando {job.task_id}...")
    job.status_message_id = status.message_id

    try:
        from .sd.client import build_txt2img_payload
        generation = load_generation_settings()
        payload = build_txt2img_payload(
            prompt=params.prompt,
            negative_prompt=params.negative_prompt,
            width=params.width,
            height=params.height,
            steps=params.steps,
            cfg_scale=params.cfg_scale,
            sampler=params.sampler,
            scheduler=params.scheduler,
            seed=params.seed,
            settings=generation,
        )
        result = await sd.txt2img(payload, payload_init=payload)
        from aiogram.types import BufferedInputFile
        for index, img_b64 in enumerate(result.images_b64):
            result_id = job.task_id if index == 0 else new_task_id()
            png = base64.b64decode(img_b64)
            filename = f"{result_id}_txt2img.png"
            (DATA_DIR / filename).write_bytes(png)

            result_params = apply_result_info(params, result.info_json, index)
            jobs.remember(result_id, result_params)

            document = BufferedInputFile(png, filename=filename)
            await bot.send_document(
                job.chat_id,
                document=document,
                caption=buttons.format_caption(result_id, "txt2img", result_params),
                reply_markup=buttons.kb_txt2img(result_id),
            )

    # asyncio.CancelledError debe propagarse para que el manager marque el future
    # como cancelado; cualquier otro error deja el status visible y le avisa al
    # usuario con un botón REINTENTAR (que vuelve a mandar el prompt original).
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("Fallo generando %s", job.task_id)
        if job.raw_prompt:
            jobs.store_retry(job.task_id, job.raw_prompt)
        if job.user_message_id is not None:
            try:
                await bot.delete_message(job.chat_id, job.user_message_id)
            except Exception:
                pass
        try:
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
            markup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔁 REINTENTAR", callback_data=Retry(task_id=job.task_id).pack())]
            ])
            err = str(e).strip() or type(e).__name__
            await bot.send_message(
                job.chat_id,
                f"❌ Falló la generación\n\n`{job.prompt}`\n\n<i>{err[:200]}</i>",
                reply_markup=markup,
            )
        except Exception:
            log.exception("No pude notificar el fallo al chat %s", job.chat_id)

    finally:
        chat_id = job.chat_id
        status_id = getattr(job, "status_message_id", None)
        if status_id is not None:
            try:
                await bot.delete_message(chat_id, status_id)
            except Exception:
                pass


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_generation_settings()  # valida/carga --preset antes de abrir conexiones
    s = get_settings().bot
    sd = SDClient(s.sd_api_url, s.sd_timeout_s, ROOT / "debug")
    # Health check rápido (no fatal: SD puede estar arrancando)
    try:
        opts = await sd.health()
        log.info("SD OK · %d opciones en %s", len(opts), s.sd_api_url)
    except Exception as e:  # noqa: BLE001
        if s.sd_fallback_api_url:
            log.warning("SD %s no responde (%s). Probando fallback %s.", s.sd_api_url, e, s.sd_fallback_api_url)
            sd = SDClient(s.sd_fallback_api_url, s.sd_timeout_s, ROOT / "debug")
            try:
                opts = await sd.health()
                log.info("SD OK · %d opciones en fallback %s", len(opts), s.sd_fallback_api_url)
            except Exception as e2:  # noqa: BLE001
                log.warning("SD fallback tampoco responde (%s). El bot arranca igual.", e2)
        else:
            log.warning("SD no responde todavía (%s). El bot arranca igual.", e)

    bot = Bot(token=os.environ["BOT_TOKEN"], default=DefaultBotProperties(parse_mode=None))
    jobs = JobManager(cache_dir=ROOT / "data" / "jobs")

    dp = Dispatcher()
    # Inyectamos deps en el contexto de los handlers (aiogram 3 idiom).
    dp["jobs"] = jobs
    dp["sd"] = sd
    dp.include_router(generate.router)
    dp.include_router(cancel.router)
    dp.include_router(buttons.router)

    worker_task = asyncio.create_task(
        jobs.worker_loop(lambda job: _handle_job(job, bot, sd, jobs)),
        name="sd-worker",
    )

    log.info("Bot arrancando · presioná Ctrl+C para bajar.")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        worker_task.cancel()
        await sd.aclose()
        try:
            await bot.session.close()
        except Exception:
            pass


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nbye.", file=sys.stderr)


if __name__ == "__main__":
    run()
