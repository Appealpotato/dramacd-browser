"""Translation quality improvements — global glossary, auto-glossary,
provider-aware chunk sizing, and the review pass."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database
from pipeline.translation_job import _apply_review_rows, _build_review_prompt
from routers.api import _build_auto_glossary_prompt, _coerce_glossary_text


class MergeGlossariesTests(unittest.TestCase):
    def test_merges_and_dedupes(self):
        merged = database.merge_glossaries(
            "ロマーナ=Romana\n小霧=Kogiri",
            "小霧=Kogiri\n智恵=Tomoe",
        )
        self.assertEqual(merged, "ロマーナ=Romana\n小霧=Kogiri\n智恵=Tomoe")

    def test_global_rules_come_first(self):
        merged = database.merge_glossaries("global=G", "item=I")
        self.assertEqual(merged.splitlines(), ["global=G", "item=I"])

    def test_blank_parts_and_lines_dropped(self):
        self.assertEqual(database.merge_glossaries("", None, "a=b\n\n  \nc=d"), "a=b\nc=d")

    def test_dedupe_is_case_insensitive_but_keeps_first_spelling(self):
        self.assertEqual(database.merge_glossaries("Name=Tomoe", "name=tomoe"), "Name=Tomoe")

    def test_all_empty(self):
        self.assertEqual(database.merge_glossaries(None, "", "   "), "")


class GlobalGlossarySettingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls._old_db_path = database.DB_PATH
        database.DB_PATH = Path(cls._tmpdir.name) / "test.db"
        asyncio.run(database.init_db())

    @classmethod
    def tearDownClass(cls):
        database.DB_PATH = cls._old_db_path
        cls._tmpdir.cleanup()

    def test_round_trip_preserves_newlines(self):
        text = "ロマーナ=Romana\n小霧幸人=Kogiri Yukito"
        asyncio.run(database.set_runtime_global_glossary(text))
        self.assertEqual(asyncio.run(database.get_runtime_global_glossary()), text)

    def test_empty_clears(self):
        asyncio.run(database.set_runtime_global_glossary("a=b"))
        asyncio.run(database.set_runtime_global_glossary(""))
        self.assertEqual(asyncio.run(database.get_runtime_global_glossary()), "")

    def test_unset_returns_empty_string(self):
        # A fresh key must read as "" not None so prompt-building never sees None.
        asyncio.run(database.set_app_setting(database.RUNTIME_GLOBAL_GLOSSARY_SETTING, ""))
        self.assertEqual(asyncio.run(database.get_runtime_global_glossary()), "")


class ReviewPassHelpersTests(unittest.TestCase):
    def test_prompt_contains_pairs_and_contract(self):
        prompt = _build_review_prompt(
            target_language="en",
            description_context="A cafe romance.",
            glossary="ロマーナ=Romana",
            pairs=[{"segment_index": 3, "ja": "おはよう", "tl": "Good morning"}],
        )
        self.assertIn('"segment_index": 3', prompt)
        self.assertIn("おはよう", prompt)
        self.assertIn("Good morning", prompt)
        self.assertIn("ロマーナ=Romana", prompt)
        self.assertIn('[{"segment_index":N,"text":"..."}]', prompt)
        self.assertIn("UNCHANGED", prompt)

    def test_apply_changes_only_differing_segments(self):
        tmap = {
            1: {"segment_index": 1, "text": "old one", "meta": {}},
            2: {"segment_index": 2, "text": "keep me", "meta": {}},
        }
        changed = _apply_review_rows(
            [1, 2],
            [
                {"segment_index": 1, "text": "new one"},
                {"segment_index": 2, "text": "keep me"},
            ],
            tmap,
        )
        self.assertEqual(changed, 1)
        self.assertEqual(tmap[1]["text"], "new one")
        self.assertTrue(tmap[1]["meta"].get("reviewed"))
        self.assertEqual(tmap[2]["text"], "keep me")
        self.assertNotIn("reviewed", tmap[2]["meta"])

    def test_missing_or_empty_rows_keep_first_pass_text(self):
        # A reviewer that drops segments or returns blanks must never lose
        # the first-pass translation.
        tmap = {1: {"segment_index": 1, "text": "original", "meta": {}}}
        changed = _apply_review_rows([1], [{"segment_index": 1, "text": "  "}], tmap)
        self.assertEqual(changed, 0)
        self.assertEqual(tmap[1]["text"], "original")
        changed = _apply_review_rows([1], [], tmap)
        self.assertEqual(changed, 0)
        self.assertEqual(tmap[1]["text"], "original")

    def test_garbage_rows_ignored(self):
        tmap = {1: {"segment_index": 1, "text": "original", "meta": {}}}
        changed = _apply_review_rows(
            [1],
            ["not a dict", {"segment_index": "x", "text": "bad index"}, None],
            tmap,
        )
        self.assertEqual(changed, 0)
        self.assertEqual(tmap[1]["text"], "original")


class AutoGlossaryTests(unittest.TestCase):
    def test_prompt_includes_metadata_fields(self):
        prompt = _build_auto_glossary_prompt(
            {
                "title": "カフェ・ロマーナへようこそ",
                "title_en": "Welcome to Cafe Romana",
                "series": "ロマーナ",
                "circle": "TeaTime",
                "seiyuu": '["茶介"]',
                "tags": '["カフェ"]',
                "description": "小霧幸人の恋の物語。",
                "description_en": "The love story of Kogiri Yukito.",
            }
        )
        self.assertIn("カフェ・ロマーナへようこそ", prompt)
        self.assertIn("茶介", prompt)
        self.assertIn("Kogiri Yukito", prompt)
        self.assertIn("never guess", prompt)
        self.assertIn("日本語=English", prompt)

    def test_prompt_survives_malformed_json_columns(self):
        prompt = _build_auto_glossary_prompt({"title": "x", "seiyuu": "not json", "tags": None})
        self.assertIn("title: x", prompt)

    def test_coerce_glossary_text_shapes(self):
        self.assertEqual(_coerce_glossary_text({"glossary": "a=b\nc=d"}), "a=b\nc=d")
        self.assertEqual(_coerce_glossary_text({"glossary": ["a=b", "c=d"]}), "a=b\nc=d")
        self.assertEqual(_coerce_glossary_text(["a=b"]), "a=b")
        self.assertEqual(_coerce_glossary_text({"glossary": ""}), "")
        self.assertEqual(_coerce_glossary_text(None), "")


class WhisperHotwordsTests(unittest.TestCase):
    def test_title_series_and_glossary_ja_sides(self):
        from pipeline.whisper_job import _build_hotwords

        hw = _build_hotwords(
            {"title": "ヤンデレお兄ちゃんは睡眠姦で妹を孕ませたい", "series": "甘い毒"},
            "小霧幸人=Kogiri Yukito\nRomana=Romana\n茶介=Chasuke",
        )
        self.assertIn("睡眠姦", hw)
        self.assertIn("甘い毒", hw)
        self.assertIn("小霧幸人", hw)
        self.assertIn("茶介", hw)
        # ASCII-only glossary left sides are useless to Whisper.
        self.assertNotIn("Romana", hw)

    def test_empty_item_returns_none(self):
        from pipeline.whisper_job import _build_hotwords

        self.assertIsNone(_build_hotwords({}, ""))
        self.assertIsNone(_build_hotwords({"title": "   "}, "ascii=only"))
        # An English title is still included — harmless context for Whisper.
        self.assertEqual(_build_hotwords({"title": "English Title"}, "a=b"), "English Title")

    def test_capped_length(self):
        from pipeline.whisper_job import _build_hotwords, _HOTWORDS_MAX_CHARS

        hw = _build_hotwords({"title": "あ" * 1000}, "")
        self.assertLessEqual(len(hw), _HOTWORDS_MAX_CHARS)

    def test_dedupes(self):
        from pipeline.whisper_job import _build_hotwords

        hw = _build_hotwords({"title": "甘い毒", "series": "甘い毒"}, "甘い毒=Sweet Poison")
        self.assertEqual(hw, "甘い毒")


class ChunkAutoSizingTests(unittest.TestCase):
    """queue_translation must store None (= auto) when no explicit chunk
    sizes are given, so translation_job can size per provider."""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls._old_db_path = database.DB_PATH
        database.DB_PATH = Path(cls._tmpdir.name) / "test.db"
        asyncio.run(database.init_db())

    @classmethod
    def tearDownClass(cls):
        database.DB_PATH = cls._old_db_path
        cls._tmpdir.cleanup()

    def test_metadata_stores_none_for_auto(self):
        from pipeline.service import queue_translation

        job_id = asyncio.run(
            queue_translation(item_id=1, track_id=2, transcript_run_id=3, review_pass=True)
        )
        job = asyncio.run(database.get_job(job_id))
        meta = job["metadata_json"]
        self.assertIsNone(meta["max_tokens_per_chunk"])
        self.assertIsNone(meta["max_lines_per_chunk"])
        self.assertTrue(meta["review_pass"])

    def test_metadata_stores_explicit_values(self):
        from pipeline.service import queue_translation

        job_id = asyncio.run(
            queue_translation(
                item_id=1, track_id=2, transcript_run_id=3,
                max_tokens_per_chunk=2500, max_lines_per_chunk=40,
            )
        )
        job = asyncio.run(database.get_job(job_id))
        meta = job["metadata_json"]
        self.assertEqual(meta["max_tokens_per_chunk"], 2500)
        self.assertEqual(meta["max_lines_per_chunk"], 40)
        self.assertNotIn("review_pass", meta)


if __name__ == "__main__":
    unittest.main()
