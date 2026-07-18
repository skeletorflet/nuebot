"""Texto libre del usuario → prompt directo al SD.

Cualquier mensaje de texto con ≥3 palabras se encola como prompt.
Comandos (mensajes que empiezan con '/') los maneja el dispatcher central.
"""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ..config import get_settings
from ..jobs.manager import Job, JobManager, JobParams, new_task_id

router = Router(name="generate")


def _is_authorized(user_id: int) -> bool:
    s = get_settings()
    return s.allowed_user_id is None or s.allowed_user_id == user_id


@router.message(Command("start", "help"))
async def cmd_help(message: Message) -> None:
    if not _is_authorized(message.from_user.id):  # type: ignore[union-attr]
        return
    await message.answer(
        "Mandame cualquier texto de al menos 3 palabras y lo dibujo.\n"
        "/cancel <id> — cancela un job\n"
        "/status — cola actual"
    )


@router.message(Command("status"))
async def cmd_status(message: Message, jobs: JobManager) -> None:
    if not _is_authorized(message.from_user.id):  # type: ignore[union-attr]
        return
    current = jobs.current_task_id()
    pending = jobs.pending_ids()
    parts: list[str] = []
    if current:
        parts.append(f"🎨 Corriendo: `{current}`")
    if pending:
        parts.append("🟡 En cola: " + ", ".join(f"`{t}`" for t in pending))
    if not parts:
        parts.append("Cola vacía.")
    await message.answer("\n".join(parts))


@router.message()
async def free_text_to_prompt(message: Message, jobs: JobManager) -> None:
    if not _is_authorized(message.from_user.id):  # type: ignore[union-attr]
        return
    text = (message.text or "").strip()
    if not text:
        return
    if text.startswith("/"):
        # Comando no matcheado por ningún router — silencio.
        return
    if len(text.split()) < 3:
        await message.answer("Mandame un prompt de al menos 3 palabras.")
        return

    s = get_settings()
    params = JobParams(
        prompt=text,
        negative_prompt=s.negative_prompt,
        width=s.default_width,
        height=s.default_height,
        steps=s.default_steps,
        cfg_scale=s.default_cfg,
        sampler=s.default_sampler,
        scheduler=s.default_scheduler,
        seed=-1,
        kind="txt2img",
    )

    task_id = new_task_id()
    job = Job(
        task_id=task_id,
        chat_id=message.chat.id,
        prompt=text,
        params=params,
    )
    pos = jobs.enqueue(job)
    await message.answer(f"🟡 Encolado como `{task_id}` (posición {pos}).")
