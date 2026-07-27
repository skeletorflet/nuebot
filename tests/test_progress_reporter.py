"""Tests del reporter de progreso: render + ciclo de vida del editor.

No queremos martillar el SD real. Mockeamos ``SDClient.progress`` para
que devuelva valores controlados y assertamos el orden de las llamadas
a ``edit_message_text`` y la limpieza cuando se interrumpe.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from aiogram.exceptions import TelegramAPIError

from nuebot.jobs.progress_reporter import (
    ProgressEditor,
    _bar,
    _fmt_eta,
    _format_progress,
)


class BarAndEtaTests(unittest.TestCase):
    def test_bar_clamps_to_bounds(self):
        self.assertEqual(_bar(0).count("█"), 0)
        self.assertEqual(_bar(100).count("█"), 20)
        # 150 -> clamp a 100
        self.assertEqual(_bar(150).count("█"), 20)
        # -5 -> clamp a 0
        self.assertEqual(_bar(-5).count("█"), 0)

    def test_bar_rounds_correctly(self):
        # 33% -> 7/20
        self.assertEqual(_bar(33).count("█"), 7)
        # 50% -> 10/20
        self.assertEqual(_bar(50).count("█"), 10)

    def test_fmt_eta(self):
        self.assertEqual(_fmt_eta(0), "0s")
        self.assertEqual(_fmt_eta(30), "30s")
        self.assertEqual(_fmt_eta(60), "1m 00s")
        self.assertEqual(_fmt_eta(90), "1m 30s")
        self.assertEqual(_fmt_eta(3661), "61m 01s")


class FormatProgressTests(unittest.TestCase):
    def test_empty_progress_returns_label_only(self):
        out = _format_progress("🎨 x", {})
        self.assertEqual(out, "🎨 x")

    def test_running_progress_includes_bar_and_step(self):
        progress = {
            "progress": 0.4,
            "eta_relative": 12.0,
            "state": {
                "sampling_step": 14,
                "sampling_steps": 28,
                "job_count": 1,
                "textinfo": "Sampler: Euler",
            },
        }
        out = _format_progress("🎨 x", progress)
        self.assertIn("40%", out)
        self.assertIn("12s", out)
        self.assertIn("step 15/28", out)  # 0-indexed -> +1
        self.assertIn("Sampler: Euler", out)

    def test_hr_progress_includes_total_label(self):
        progress = {
            "progress": 0.7,
            "eta_relative": 9.0,
            "state": {"sampling_step": 9, "sampling_steps": 14, "job_count": 1},
        }
        out = _format_progress(
            "✨ HR",
            progress,
            total_steps_label="8 base + 4 HR (50%)",
        )
        self.assertIn("8 base + 4 HR (50%)", out)
        self.assertIn("70%", out)
        self.assertIn("step 10/14", out)


class ProgressEditorLifecycleTests(unittest.TestCase):
    def _make_bot(self):
        bot = MagicMock()
        bot.edit_message_text = AsyncMock()
        return bot

    def _make_sd(self, responses):
        """``responses`` es una lista de dicts que va entregando SDClient.progress."""
        sd = MagicMock()
        sd.progress = AsyncMock(side_effect=responses)
        return sd

    async def _drain(self, task, seconds=0.05):
        await asyncio.sleep(seconds)
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def test_runs_first_poll_then_edits_message(self):
        async def scenario():
            bot = self._make_bot()
            # side_effect cíclico: siempre devuelve lo mismo ya corriendo.
            sd = MagicMock()
            sd.progress = AsyncMock(return_value={
                "progress": 0.5, "eta_relative": 5.0,
                "state": {"sampling_step": 5, "sampling_steps": 10, "job_count": 1},
            })
            ed = ProgressEditor(
                bot=bot,
                chat_id=42,
                status_message_id=7,
                label="🎨 x",
                sd=sd,
                interval=0.01,
            )
            ed_task = asyncio.create_task(ed.run())
            await asyncio.sleep(0.1)
            ed.request_stop()
            await asyncio.wait_for(ed_task, timeout=1.0)
            self.assertGreaterEqual(bot.edit_message_text.await_count, 1)
            # El primer edit debe incluir la barra con el 50%.
            first_kwargs = bot.edit_message_text.call_args_list[0].kwargs
            self.assertIn("50%", first_kwargs["text"])
            self.assertEqual(first_kwargs["chat_id"], 42)
            self.assertEqual(first_kwargs["message_id"], 7)

        asyncio.run(scenario())

    def test_interrupted_state_shows_cancelado_and_stops(self):
        async def scenario():
            bot = self._make_bot()
            sd = self._make_sd([
                {"progress": 0.3, "eta_relative": 7.0,
                 "state": {"sampling_step": 3, "sampling_steps": 10, "job_count": 1,
                          "interrupted": True}},
            ])
            ed = ProgressEditor(
                bot=bot, chat_id=1, status_message_id=2, label="🎨 x",
                sd=sd, interval=0.01,
            )
            ed_task = asyncio.create_task(ed.run())
            await asyncio.wait_for(ed_task, timeout=1.0)
            last_kwargs = bot.edit_message_text.call_args.kwargs
            self.assertIn("❌ cancelado", last_kwargs["text"])

        asyncio.run(scenario())

    def test_edit_not_modified_is_silent(self):
        async def scenario():
            bot = MagicMock()
            sd = MagicMock()
            sd.progress = AsyncMock(return_value={
                "progress": 0.5, "eta_relative": 5.0,
                "state": {"sampling_step": 5, "sampling_steps": 10, "job_count": 1},
            })
            # "message is not modified" en cada edit -> el editor no debe
            # crashear. Cada poll genera UN intento de edit (mismo texto).
            not_modified = TelegramAPIError(
                method="editMessageText",
                message="Bad Request: message is not modified",
            )
            bot.edit_message_text = AsyncMock(side_effect=not_modified)
            ed = ProgressEditor(
                bot=bot, chat_id=1, status_message_id=2, label="🎨 x",
                sd=sd, interval=0.01,
            )
            ed_task = asyncio.create_task(ed.run())
            await asyncio.sleep(0.1)
            ed.request_stop()
            await asyncio.wait_for(ed_task, timeout=1.0)
            # Si llegó al menos a una edición sin crashear, OK.
            self.assertGreaterEqual(bot.edit_message_text.await_count, 1)

        asyncio.run(scenario())

    def test_edit_other_failure_marks_stopped_does_not_crash(self):
        async def scenario():
            bot = MagicMock()
            sd = MagicMock()
            sd.progress = AsyncMock(return_value={
                "progress": 0.1, "eta_relative": 9.0,
                "state": {"sampling_step": 1, "sampling_steps": 10, "job_count": 1},
            })
            # TelegramAnyError genérico (no es "not modified"): editor marca stop.
            bot.edit_message_text = AsyncMock(side_effect=TelegramAPIError(
                method="editMessageText", message="Bad Request: chat not found",
            ))
            ed = ProgressEditor(
                bot=bot, chat_id=1, status_message_id=2, label="🎨 x",
                sd=sd, interval=0.01,
            )
            ed_task = asyncio.create_task(ed.run())
            await asyncio.wait_for(ed_task, timeout=1.0)
            # Terminó solo (no crashea).
            self.assertTrue(ed_task.done())

        asyncio.run(scenario())

    def test_sd_progress_timeout_returns_empty(self):
        """``SDClient.progress()`` retorna {} en timeout y el editor sigue vivo."""
        async def scenario():
            bot = self._make_bot()
            sd = MagicMock()
            sd.progress = AsyncMock(return_value={})  # simula timeout
            ed = ProgressEditor(
                bot=bot, chat_id=1, status_message_id=2, label="🎨 x",
                sd=sd, interval=0.01,
            )
            ed_task = asyncio.create_task(ed.run())
            await asyncio.sleep(0.1)
            ed.request_stop()
            await asyncio.wait_for(ed_task, timeout=1.0)
            # Con progress={} y last_text vacío, la primera vuelta edita
            # el label solo. Después cada poll subsiguiente también entra
            # a la rama "label solo" y como matchea con _last_text, no
            # vuelve a editar. El editor no crashea.
            self.assertGreaterEqual(bot.edit_message_text.await_count, 1)
            first_kwargs = bot.edit_message_text.call_args_list[0].kwargs
            self.assertEqual(first_kwargs["text"], "🎨 x")

        asyncio.run(scenario())


class SDClientProgressMethodTests(unittest.TestCase):
    """Valida que SDClient.progress() maneja errores HTTP sin tirar."""

    def test_progress_handles_timeoutexception(self):
        async def scenario():
            import httpx
            from nuebot.sd.client import SDClient

            client = httpx.AsyncClient(
                base_url="http://127.0.0.1:9",  # puerto muerto
                timeout=httpx.Timeout(0.1, connect=0.05),
            )
            sd = SDClient.__new__(SDClient)
            sd._client = client
            # Forzamos TimeoutException en lugar de ConnectError.
            from unittest.mock import patch
            with patch.object(client, "get", side_effect=httpx.TimeoutException("boom")):
                result = await sd.progress()
            self.assertEqual(result, {})
            await client.aclose()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()


class ApoptosisTests(unittest.IsolatedAsyncioTestCase):
    """Si progress no avanza por _STUCK_TICKS ticks y job_count>0, el editor
    llama sd.interrupt() y marca el mensaje como 'sin avance'."""

    async def test_apoptosis_fires_after_stuck_ticks(self):
        from nuebot.jobs.progress_reporter import ProgressEditor, _STUCK_TICKS

        sd = MagicMock()
        sd.progress = AsyncMock(return_value={
            "progress": 0.42,
            "eta_relative": 30.0,
            "state": {"job_count": 1, "sampling_step": 10, "sampling_steps": 28},
        })
        sd.interrupt = AsyncMock()

        bot = MagicMock()
        bot.edit_message_text = AsyncMock()

        editor = ProgressEditor(
            bot=bot, chat_id=1, status_message_id=42,
            label="🎨 x", sd=sd, interval=0.001,
        )
        # Forzamos el threshold a un valor chiquito para que el test sea rápido.
        from nuebot.jobs import progress_reporter as pr_mod
        original_stuck = pr_mod._STUCK_TICKS
        pr_mod._STUCK_TICKS = 3
        try:
            # Run breve en task; cancelamos al primer apoptosis.
            task = asyncio.create_task(editor.run())
            # Esperamos ticks suficientes para que apoptosis dispare.
            for _ in range(50):
                if sd.interrupt.called:
                    break
                await asyncio.sleep(0.01)
            editor.request_stop()
            try:
                await asyncio.wait_for(task, timeout=1.0)
            except asyncio.TimeoutError:
                task.cancel()
        finally:
            pr_mod._STUCK_TICKS = original_stuck

        sd.interrupt.assert_awaited()
        # El último edit debe contener "sin avance"
        last_call = bot.edit_message_text.call_args_list[-1]
        sent_text = last_call.kwargs.get("text") or last_call.args[-1]
        self.assertIn("sin avance", sent_text)

    async def test_no_apoptosis_when_progress_advances(self):
        from nuebot.jobs.progress_reporter import ProgressEditor
        sd = MagicMock()
        progresses = iter([
            {"progress": 0.1, "state": {"job_count": 1}},
            {"progress": 0.2, "state": {"job_count": 1}},
            {"progress": 0.3, "state": {"job_count": 1}},
            {"progress": 0.4, "state": {"job_count": 1}},
            {"progress": 0.5, "state": {"job_count": 1}},
            {"progress": 1.0, "state": {"job_count": 0}},
        ])
        async def _next_p():
            try:
                return next(progresses)
            except StopIteration:
                return {"progress": 1.0, "state": {"job_count": 0}}
        sd.progress = AsyncMock(side_effect=_next_p)
        sd.interrupt = AsyncMock()
        bot = MagicMock()
        bot.edit_message_text = AsyncMock()

        editor = ProgressEditor(
            bot=bot, chat_id=1, status_message_id=42,
            label="🎨 x", sd=sd, interval=0.001,
        )
        task = asyncio.create_task(editor.run())
        await asyncio.sleep(0.05)
        editor.request_stop()
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except asyncio.TimeoutError:
            task.cancel()
        sd.interrupt.assert_not_called()
