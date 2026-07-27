from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nuebot.handlers.generate import expand_resource_tokens


class ResourcePromptTests(unittest.TestCase):
    def test_resource_token_picks_one_line_and_reports_wildcard(self):
        with tempfile.TemporaryDirectory() as tmp:
            resources = Path(tmp)
            lines = [f"option-{index}" for index in range(13)]
            (resources / "f_anime.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

            # ponytail: random.choice (no random.sample) sobre el archivo,
            # y devuelve (prompt, had_wildcards) para que el handler sepa
            # si debe clonar el POST N veces con prompts distintos.
            with patch.object(random, "choice", return_value=lines[5]) as choice:
                prompt, had_wildcards = expand_resource_tokens(
                    "portrait f_anime cinematic", resources
                )

            self.assertEqual(prompt, f"portrait {lines[5]} cinematic")
            self.assertTrue(had_wildcards)
            choice.assert_called_once_with(lines)

    def test_no_wildcard_returns_unchanged_prompt_and_false_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            resources = Path(tmp)
            prompt, had_wildcards = expand_resource_tokens(
                "un angel en un bosque sin tokens", Path(tmp)
            )
            self.assertEqual(prompt, "un angel en un bosque sin tokens")
            self.assertFalse(had_wildcards)


if __name__ == "__main__":
    unittest.main()


class PromptCapTests(unittest.IsolatedAsyncioTestCase):
    """El cap MAX_PROMPT_BYTES evita expansión DoS antes de encolar."""

    async def test_enqueue_raises_when_expanded_prompt_exceeds_cap(self):
        from nuebot.handlers.generate import _enqueue_prompt, MAX_PROMPT_BYTES

        class _FakeJobs:
            def enqueue(self, job):  # pragma: no cover - shouldn't be called
                raise AssertionError("enqueue no debería dispararse si el cap rechaza")

        huge = "x " * (MAX_PROMPT_BYTES // 2 + 100)  # >> cap
        with self.assertRaises(ValueError) as ctx:
            await _enqueue_prompt(1, huge, _FakeJobs(), 1)  # type: ignore[arg-type]
        self.assertIn("demasiado largo", str(ctx.exception))

    async def test_enqueue_accepts_normal_prompt(self):
        from nuebot.handlers.generate import _enqueue_prompt

        captured = {}

        class _FakeJobs:
            def enqueue(self, job):
                captured["job"] = job
                return 1

        await _enqueue_prompt(1, "tres palabras simples aqui", _FakeJobs(), 1)  # type: ignore[arg-type]
        self.assertIn("job", captured)


class CharacterTokenAnchorsTests(unittest.TestCase):
    """Las líneas de r_female/r_male arrancan con anchors Danbooru
    (1girl/1boy, solo, looking at viewer) — sin ellos Pony/NoobAI
    tiende a meter personajes secundarios de la misma serie."""

    def test_female_lines_anchor_one_girl_solo(self):
        lines = [ln for ln in Path(__file__).resolve().parents[1].joinpath("resources", "r_female.txt").read_text(encoding="utf-8").splitlines() if ln.strip()]
        bad = [ln[:60] for ln in lines if not ln.startswith("1girl, solo, looking at viewer")]
        self.assertEqual(bad, [], msg=f"{len(bad)} lines without anchor: {bad[:3]}")

    def test_male_lines_anchor_one_boy_solo(self):
        lines = [ln for ln in Path(__file__).resolve().parents[1].joinpath("resources", "r_male.txt").read_text(encoding="utf-8").splitlines() if ln.strip()]
        bad = [ln[:60] for ln in lines if not ln.startswith("1boy, solo, looking at viewer")]
        self.assertEqual(bad, [], msg=f"{len(bad)} lines without anchor: {bad[:3]}")


class ExpandResourceTokensTests(unittest.TestCase):
    """Cubre el cambio crítico: random.choice por archivo + flag had_wildcards
    + evaluación de {a|b|c} literal. Sin esto, n_iter>1 con un token r_*
    daría 4 imágenes casi-idénticas (A1111 no randomiza por iteración)."""

    def test_literal_braces_are_resolved_not_left_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            prompt, had = expand_resource_tokens("un {perro|gato|pez} azul", Path(tmp))
        # tenía {a|b|c} → lo evaluó y reporta had_wildcards=True
        self.assertIn(prompt, {"un perro azul", "un gato azul", "un pez azul"})
        self.assertNotIn("{", prompt)
        self.assertTrue(had)

    def test_repeated_resource_token_keeps_flag_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "r_place.txt").write_text("ciudad\nbosque\nplaya\n", encoding="utf-8")
            prompt, had = expand_resource_tokens("r_place con cosas", Path(tmp))
        self.assertNotIn("r_place", prompt)
        self.assertIn(prompt, {"ciudad con cosas", "bosque con cosas", "playa con cosas"})
        self.assertTrue(had)

    def test_empty_braces_are_stripped(self):
            with tempfile.TemporaryDirectory() as tmp:
                prompt, had = expand_resource_tokens("hola {|} mundo", Path(tmp))
            # {|} → no hay opciones válidas (split("|") → ["", ""], ambas stripped → [])
            # El bloque se strippea entero, sin forzar had_wildcards=True.
            self.assertEqual(prompt, "hola  mundo")
            self.assertFalse(had)
