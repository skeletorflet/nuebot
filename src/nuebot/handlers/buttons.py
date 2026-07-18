"""Botones inline: Repetir / HR Upscale / Upscale Final.

Callback_data factory de aiogram 3 — type-safe y dentro del límite de 64 bytes.

Los resultados nuevos se envían como documentos para que Telegram no los
trate como fotos comprimidas. Sus parámetros completos se conservan en el
cache de jobs; el caption queda legible y los captions técnicos antiguos
siguen siendo compatibles.
"""
from __future__ import annotations

import base64
from dataclasses import replace

from aiogram import Bot, F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from ..config import load_generation_settings
from ..jobs.manager import JobManager, JobParams, apply_result_info, new_task_id
from ..sd.client import (
    build_extra_payload,
    build_hr_block,
    build_txt2img_payload,
)

router = Router(name="buttons")


# --- CallbackData -----------------------------------------------------------

class Repeat(CallbackData, prefix="rep"):
    task_id: str


class HR(CallbackData, prefix="up"):
    task_id: str


class FinalUpscale(CallbackData, prefix="upx"):
    task_id: str


# --- Keyboards --------------------------------------------------------------

def kb_txt2img(task_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✨ UPSCALE", callback_data=HR(task_id=task_id).pack()),
            InlineKeyboardButton(text="🔄 REPETIR", callback_data=Repeat(task_id=task_id).pack()),
        ]
    ])


def kb_hires(task_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✨ UPSCALE FINAL", callback_data=FinalUpscale(task_id=task_id).pack()),
            InlineKeyboardButton(text="🔄 REPETIR", callback_data=Repeat(task_id=task_id).pack()),
        ]
    ])


def kb_final(task_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 REPETIR", callback_data=Repeat(task_id=task_id).pack())]
    ])


# --- Caption visible + compatibilidad con captions antiguos -----------------

# Captions antiguos todavía podían contener el estado serializado. Los nuevos
# documentos usan el cache persistente de JobManager y no muestran este bloque.
_PARAM_SEP = "\x1f"
PARAM_PREFIX = "⚙ "


def format_caption(task_id: str, kind_label: str, params: JobParams) -> str:
    """Formato de documento del screenshot.

    El estado completo se guarda en JobManager (RAM + JSON). No agregamos el
    bloque técnico al caption porque el usuario pidió un mensaje limpio como
    el screenshot; el parser de abajo queda para botones de mensajes viejos.
    """
    width = params.display_width or params.width
    height = params.display_height or params.height
    head = "✅ 🎨 Generación completada\n📝 Prompt:\n"
    config = (
        "⚙️ Configuración:\n"
        f"• Pasos: {params.steps}\n"
        f"• Sampler: {params.sampler}\n"
        f"• Scheduler: {params.scheduler}\n"
        f"• CFG: {params.cfg_scale:g}\n"
        f"• Seed: {params.seed}\n"
        f"• Tamaño: {width}x{height}\n"
        f"👤 Autor: {params.author}"
    )
    # Telegram limits document captions to 1024 chars. Preserve the complete
    # prompt in the JSON cache and truncate only the visual copy if necessary.
    prompt = params.prompt
    available = max(0, 1024 - len(head) - len(config) - 1)
    if len(prompt) > available:
        prompt = prompt[: max(0, available - 1)].rstrip() + "…"
    return f"{head}{prompt}\n{config}"


def parse_params_from_caption(caption: str | None) -> JobParams | None:
    """Lee captions técnicos de versiones anteriores del bot.

    Los captions nuevos se mantienen visualmente limpios y se recuperan desde
    el cache JSON por task_id; por eso no intentamos reconstruir el negative
    prompt desde el texto visible.
    """
    if not caption:
        return None
    line = next((l for l in caption.splitlines() if l.startswith(PARAM_PREFIX)), None)
    if line is None:
        return None
    raw = line[len(PARAM_PREFIX):]
    try:
        # split("=", 1) en cada par clave=valor para tolerar '=' dentro del valor.
        fields = dict(kv.split("=", 1) for kv in raw.split(_PARAM_SEP) if "=" in kv)
        return JobParams(
            prompt=fields["p"],
            negative_prompt=fields["n"],
            width=int(fields["w"]),
            height=int(fields["h"]),
            steps=int(fields["s"]),
            cfg_scale=float(fields["c"]),
            sampler=fields["sm"],
            scheduler=fields["sc"],
            seed=int(fields["sd"]),
            kind=fields.get("k", "txt2img"),
        )
    except (KeyError, ValueError):
        return None


def _resolve_params(callback: CallbackQuery, jobs: JobManager, task_id: str) -> JobParams | None:
    """Cache RAM primero; luego JSON persistente; al final caption antiguo."""
    cached = jobs.get_params(task_id)
    if cached is not None:
        return cached
    # Captions nuevos no incluyen task_id ni estado técnico: se resuelven por
    # JSON. Este fallback conserva botones de mensajes antiguos del bot.
    parsed = parse_params_from_caption(callback.message.caption)  # type: ignore[union-attr]
    if parsed is not None:
        jobs.remember(task_id, parsed)
    return parsed


# --- Helpers de ejecución ---------------------------------------------------

async def _run_txt2img(
    bot: Bot,
    jobs: JobManager,
    sd,
    chat_id: int,
    params: JobParams,
    *,
    with_hr: bool,
) -> tuple[str, bytes] | None:
    """Devuelve (task_id_nuevo, png_bytes). None si falla."""
    generation = load_generation_settings()
    hr_block = build_hr_block(steps=params.steps, settings=generation) if with_hr else None
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
        n_iter=1,
        hr=hr_block,
        settings=generation,
    )
    result = await sd.txt2img(payload, payload_init=payload)
    img_b64 = result.images_b64[0]
    png = base64.b64decode(img_b64)

    new_id = new_task_id()
    new_params = apply_result_info(params, result.info_json)
    new_params = replace(new_params, kind="hires" if with_hr else "txt2img")
    jobs.remember(new_id, new_params)

    kind_label = "HR" if with_hr else "txt2img"
    caption = format_caption(new_id, kind_label, new_params)
    document = BufferedInputFile(png, filename=f"{new_id}_{kind_label}.png")
    markup = kb_hires(new_id) if with_hr else kb_txt2img(new_id)
    await bot.send_document(chat_id, document=document, caption=caption, reply_markup=markup)
    return new_id, png


# --- Handlers ---------------------------------------------------------------

@router.callback_query(Repeat.filter())
async def on_repeat(callback: CallbackQuery, callback_data: Repeat, bot: Bot,
                    jobs: JobManager, sd) -> None:
    await callback.answer("🔁 Repetiendo...")
    params = _resolve_params(callback, jobs, callback_data.task_id)
    if params is None:
        await callback.message.answer(  # type: ignore[union-attr]
            f"⚠️ No encuentro los params de `{callback_data.task_id}`. "
            f"¿Es una imagen vieja sin bloque ⚙ en el caption?"
        )
        return
    # ponytail: REPETIR conserva todo el snapshot salvo seed para variar el resultado.
    params = replace(params, seed=-1)
    await _run_txt2img(bot, jobs, sd, callback.message.chat.id, params, with_hr=False)


@router.callback_query(HR.filter())
async def on_hr(callback: CallbackQuery, callback_data: HR, bot: Bot,
                jobs: JobManager, sd) -> None:
    await callback.answer("✨ HR Upscale...")
    params = _resolve_params(callback, jobs, callback_data.task_id)
    if params is None:
        await callback.message.answer(  # type: ignore[union-attr]
            f"⚠️ No encuentro los params de `{callback_data.task_id}`. "
            f"¿Es una imagen vieja sin bloque ⚙ en el caption?"
        )
        return
    await _run_txt2img(bot, jobs, sd, callback.message.chat.id, params, with_hr=True)


@router.callback_query(FinalUpscale.filter())
async def on_final(callback: CallbackQuery, callback_data: FinalUpscale,
                   bot: Bot, jobs: JobManager, sd) -> None:
    await callback.answer("💎 Upscale Final...")
    params = _resolve_params(callback, jobs, callback_data.task_id)
    if params is None:
        await callback.message.answer(  # type: ignore[union-attr]
            f"⚠️ No encuentro los params de `{callback_data.task_id}`. "
            f"¿Es una imagen vieja sin bloque ⚙ en el caption?"
        )
        return
    generation = load_generation_settings()
    # Re-generamos con los mismos params en txt2img normal (rápido) y después
    # le pasamos la imagen al upscaler extra. Es la única forma de tener un
    # b64 base sin pedirle al usuario que la resubmita.
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
        n_iter=1,
        settings=generation,
    )
    result = await sd.txt2img(payload, payload_init=payload)
    img_b64 = result.images_b64[0]

    up_payload = build_extra_payload(image_b64=img_b64, settings=generation)
    up_b64 = await sd.extra_single_image(up_payload)
    import base64
    png = base64.b64decode(up_b64)

    new_id = new_task_id()
    final_params = apply_result_info(params, result.info_json)
    final_params = replace(final_params, kind="final")
    jobs.remember(new_id, final_params)
    caption = format_caption(new_id, f"Final x{generation.final_upscale['upscaling_resize']}", final_params)
    document = BufferedInputFile(png, filename=f"{new_id}_final.png")
    await bot.send_document(
        callback.message.chat.id,
        document=document,
        caption=caption,
        reply_markup=kb_final(new_id),
    )
