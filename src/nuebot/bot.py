"""Bot entrypoint.

Levanta el Dispatcher, registra routers, abre el SDClient, arranca el worker
de la cola, hace polling. Ctrl+C lo baja limpio.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import signal
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from .config import get_settings
from .handlers import buttons, cancel, generate
from .jobs.manager import Job, JobManager, apply_result_info
from .sd.client import SDClient

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data" / "outputs"
DATA_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("nuebot")


async def _handle_job(job: Job, bot: Bot, sd: SDClient, jobs: JobManager) -> None:
    """Handler del worker. Convierte un Job en una foto enviada al chat."""
    s = get_settings()
    params = job.params
    assert params is not None, "enqueue garantiza params"

    if getattr(job, "_cancelled", False):
        return

    status = await bot.send_message(job.chat_id, f"🎨 Generando {job.task_id}...")

    try:
        from .sd.client import build_txt2img_payload
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
        )
        result = await sd.txt2img(payload)
        img_b64 = result.images_b64[0]
        png = base64.b64decode(img_b64)
        (DATA_DIR / f"{job.task_id}_txt2img.png").write_bytes(png)

        result_params = apply_result_info(params, result.info_json)
        jobs.remember(job.task_id, result_params)

        # El documento conserva PNG original sin recompresión de Telegram.
        from aiogram.types import BufferedInputFile
        document = BufferedInputFile(png, filename=f"{job.task_id}_txt2img.png")
        await bot.send_document(
            job.chat_id,
            document=document,
            caption=buttons.format_caption(job.task_id, "txt2img", result_params),
            reply_markup=buttons.kb_txt2img(job.task_id),
        )
        try:
            await status.delete()
        except Exception:
            pass

    except asyncio.CancelledError:
        try:
            await bot.send_message(job.chat_id, f"❌ Cancelado {job.task_id}.")
        except Exception:
            pass
        raise


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    s = get_settings()
    log.info("Conectando a SD en %s", s.sd_api_url)
    sd = SDClient(s.sd_api_url, s.sd_timeout_s)

    # Health check rápido (no fatal: SD puede estar arrancando)
    try:
        opts = await sd.health()
        log.info("SD OK · %d opciones", len(opts))
    except Exception as e:  # noqa: BLE001
        log.warning("SD no responde todavía (%s). El bot arranca igual.", e)

    bot = Bot(token=s.bot_token, default=DefaultBotProperties(parse_mode=None))
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
