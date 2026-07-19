"""Texto libre del usuario → prompt directo al SD.

Cualquier mensaje de texto con ≥3 palabras se encola como prompt.
Comandos (mensajes que empiezan con '/') los maneja el dispatcher central.
"""
from __future__ import annotations

import random
import re
from pathlib import Path

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ..config import get_settings, load_generation_settings
from ..jobs.manager import Job, JobManager, JobParams, new_task_id

router = Router(name="generate")
RESOURCES_DIR = Path(__file__).resolve().parents[2] / "resources"


def expand_resource_tokens(prompt: str, resources_dir: Path = RESOURCES_DIR) -> str:
    """Reemplaza cada token con stem de *.txt por una línea aleatoria no vacía."""
    for path in resources_dir.glob("*.txt"):
        if not re.search(rf"(?<!\w){re.escape(path.stem)}(?!\w)", prompt):
            continue
        lines = list(dict.fromkeys(
            line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        ))
        if lines:
            options = "{" + "|".join(random.sample(lines, min(4, len(lines)))) + "}"
            prompt = re.sub(rf"(?<!\w){re.escape(path.stem)}(?!\w)", lambda _: options, prompt)
    return prompt


def _is_authorized(user_id: int) -> bool:
    bot = get_settings().bot
    return bot.allowed_user_id is None or bot.allowed_user_id == user_id


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


async def _enqueue_prompt(chat_id: int, raw: str, jobs: JobManager, user_message_id: int) -> Job:
    text = expand_resource_tokens(raw.strip())
    if not text or len(text.split()) < 3:
        raise ValueError("prompt demasiado corto")
    generation = load_generation_settings().txt2img
    params = JobParams(
        prompt=text,
        negative_prompt=generation.negative_prompt,
        width=generation.width,
        height=generation.height,
        steps=generation.steps,
        cfg_scale=generation.cfg_scale,
        sampler=generation.sampler_name,
        scheduler=generation.scheduler,
        seed=generation.seed,
        kind="txt2img",
    )
    task_id = new_task_id()
    job = Job(
        task_id=task_id,
        chat_id=chat_id,
        prompt=text,
        params=params,
        user_message_id=user_message_id,
        raw_prompt=raw.strip(),
    )
    jobs.enqueue(job)
    return job


@router.message()
async def free_text_to_prompt(message: Message, jobs: JobManager) -> None:
    if not _is_authorized(message.from_user.id):  # type: ignore[union-attr]
        return
    raw = (message.text or "").strip()
    if not raw or raw.startswith("/"):
        return
    if len(raw.split()) < 3:
        await message.answer("Mandame un prompt de al menos 3 palabras.")
        return
    try:
        await _enqueue_prompt(message.chat.id, raw, jobs, message.message_id)
    except ValueError:
        await message.answer("Mandame un prompt de al menos 3 palabras.")
