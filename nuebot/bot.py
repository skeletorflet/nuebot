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

    # Lanzamos el editor de progreso en vivo para que el mensaje de
    # status muestre la barra y ETA reales del WebUI. Se cierra en el
    # finally junto con el borrado del mensaje.
    from .jobs.progress_reporter import ProgressEditor
    editor = ProgressEditor(
        bot=bot,
        chat_id=job.chat_id,
        status_message_id=status.message_id,
        label=f"🎨 Generando {job.task_id} (txt2img)",
        sd=sd,
        interval=3.0,
        total_steps_label=f"{params.steps} pasos",
    )
    editor_task = asyncio.create_task(editor.run(), name=f"progress-{job.task_id}")

    try:
        from .sd.client import build_txt2img_payload
        generation = load_generation_settings()
        await sd.post_options(generation.post_options)

        from aiogram.types import BufferedInputFile
        from .handlers.generate import expand_resource_tokens

        # ponytail: si el prompt traía wildcards (r_female, r_male, o
        # {a|b|c} literal) y el preset tiene n_iter>1, mandamos N POSTs
        # con prompts independientes. A1111 NO randomiza por iteración —
        # un único POST con n_iter=4 repetiría la misma imagen 4 veces.
        # Sin wildcards, dejamos el comportamiento nativo (1 POST, n_iter
        # del preset, sólo varía el seed entre imágenes).
        from .config import load_generation_settings as _lgs
        settings_obj = _lgs()
        n_iter = int(generation.txt2img.n_iter or 1)
        variants = n_iter if (job.had_wildcards and n_iter > 1) else 1
        all_results: list[tuple[str, bytes, dict]] = []
        for variant_index in range(variants):
            # Re-expandimos fresh en cada POST: cada variante debe ser
            # distinta. Si no hay wildcards esto es no-op (mismo prompt).
            prompt_for_post = params.prompt
            if job.had_wildcards:
                raw_prompt = job.raw_prompt or params.prompt
                prompt_for_post = raw_prompt
                if settings_obj.prompt_prefix:
                    # ponytail: el prefix se prependió al enqueue. Si lo
                    # re-prependemos en cada variante y re-expandimos,
                    # cada POST genera una elección random distinta.
                    prompt_for_post = f"{settings_obj.prompt_prefix}, {prompt_for_post}"
                prompt_for_post, _ = expand_resource_tokens(prompt_for_post)
            payload = build_txt2img_payload(
                prompt=prompt_for_post,
                negative_prompt=params.negative_prompt,
                width=params.width,
                height=params.height,
                steps=params.steps,
                cfg_scale=params.cfg_scale,
                sampler=params.sampler,
                scheduler=params.scheduler,
                seed=-1,  # cada POST con seed fresco
                n_iter=1,  # variantes vienen del outer loop
                settings=generation,
            )
            result = await sd.txt2img(payload, payload_init=payload)
            for index, img_b64 in enumerate(result.images_b64):
                all_results.append((prompt_for_post, base64.b64decode(img_b64), result.info_json))

        total = len(all_results)
        for index, (used_prompt, png, info_json) in enumerate(all_results):
            if getattr(job, "_cancelled", False):
                break
            result_id = new_task_id()
            filename = f"{result_id}_txt2img.png"
            (DATA_DIR / filename).write_bytes(png)

            # Si expandimos para esta variante, persistimos el prompt final
            # para que Repetir conserve esa versión específica.
            variant_params = params
            if used_prompt != params.prompt:
                from dataclasses import replace
                variant_params = replace(params, prompt=used_prompt)
            result_params = apply_result_info(variant_params, info_json, index)
            jobs.remember(result_id, result_params)

            document = BufferedInputFile(png, filename=filename)
            caption = buttons.format_caption(
                result_id, "txt2img", result_params,
                variant=f"{index + 1}/{total}" if total > 1 else None,
            )
            await bot.send_document(
                job.chat_id,
                document=document,
                caption=caption,
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
        # Cerramos el editor de progreso antes de borrar el mensaje, si
        # no podría intentar editar un mensaje que ya no existe.
        editor.request_stop()
        try:
            await asyncio.wait_for(editor_task, timeout=2.0)
        except asyncio.TimeoutError:
            editor_task.cancel()
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