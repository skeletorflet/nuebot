"""/cancel <task_id> — corta el job actual o lo saca de la cola."""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ..jobs.manager import JobManager
from .generate import _is_authorized

router = Router(name="cancel")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, jobs: JobManager, sd) -> None:  # sd: SDClient
    if not _is_authorized(message.from_user.id):  # type: ignore[union-attr]
        return
    args = (message.text or "").split()
    if len(args) < 2:
        await message.answer("Uso: `/cancel <task_id>`")
        return
    task_id = args[1].strip().strip("`")
    msg = await jobs.cancel(task_id, interrupt_fn=sd.interrupt)
    await message.answer(msg)
