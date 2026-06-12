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

    def test_conflicting_term_mappings_first_wins(self):
        # The feedback loop piled up three different spellings for one series
        # name; rule lines must dedupe by TERM, not by whole line.
        merged = database.merge_glossaries(
            "濡恋鬼譚=Nurekoi Kitan",
            "濡恋鬼譚=Nurengi Kitan\nミヤ=Miya",
            "ミヤ=Miyu-kun",
        )
        self.assertEqual(merged.splitlines(), ["濡恋鬼譚=Nurekoi Kitan", "ミヤ=Miya"])

    def test_non_rule_lines_still_dedupe_by_line(self):
        # Character memory lines (no '=') keep whole-line semantics.
        merged = database.merge_glossaries("Miya: teasing tone", "Miya: teasing tone\nListener: an OL")
        self.assertEqual(merged.splitlines(), ["Miya: teasing tone", "Listener: an OL"])

    def test_dangling_equals_treated_as_plain_line(self):
        merged = database.merge_glossaries("=missing left", "trailing=", "=missing left")
        self.assertEqual(merged.splitlines(), ["=missing left", "trailing="])


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


class CharacterMemoryDbTests(unittest.TestCase):
    """Migration 029 column + accessors, including the missing-item contract
    (None/False) the endpoints rely on for 404s."""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls._old_db_path = database.DB_PATH
        database.DB_PATH = Path(cls._tmpdir.name) / "test.db"
        asyncio.run(database.init_db())

        async def _insert_item():
            db = await database.get_db()
            try:
                cursor = await db.execute(
                    "INSERT INTO items (product_code, title) VALUES ('RJ999001', 'テスト')"
                )
                await db.commit()
                return cursor.lastrowid
            finally:
                await db.close()

        cls.item_id = asyncio.run(_insert_item())

    @classmethod
    def tearDownClass(cls):
        database.DB_PATH = cls._old_db_path
        cls._tmpdir.cleanup()

    def test_round_trip_preserves_newlines(self):
        text = "幸人: rough, teasing older brother\nListener: younger sister"
        self.assertTrue(asyncio.run(database.set_item_character_memory(self.item_id, text)))
        self.assertEqual(asyncio.run(database.get_item_character_memory(self.item_id)), text)

    def test_fresh_item_reads_empty_string(self):
        async def _insert():
            db = await database.get_db()
            try:
                cursor = await db.execute("INSERT INTO items (product_code) VALUES ('RJ999002')")
                await db.commit()
                return cursor.lastrowid
            finally:
                await db.close()

        new_id = asyncio.run(_insert())
        self.assertEqual(asyncio.run(database.get_item_character_memory(new_id)), "")

    def test_missing_item_contract(self):
        self.assertIsNone(asyncio.run(database.get_item_character_memory(99999999)))
        self.assertFalse(asyncio.run(database.set_item_character_memory(99999999, "x")))


class TranslationJobItemFallbackTests(unittest.TestCase):
    """Task A coherence fix: jobs queued without a glossary must still merge
    the item's saved glossary + character memory from the DB (global → item →
    job precedence). Tested at the merge layer the job code calls."""

    def test_merge_order_global_item_job(self):
        merged = database.merge_glossaries("g=G", "i=I", "j=J")
        self.assertEqual(merged.splitlines(), ["g=G", "i=I", "j=J"])

    def test_job_rules_dedupe_against_item(self):
        merged = database.merge_glossaries("", "小霧=Kogiri", "小霧=kogiri\n茅=Chigaya")
        self.assertEqual(merged.splitlines(), ["小霧=Kogiri", "茅=Chigaya"])


class AutoCharacterMemoryTests(unittest.TestCase):
    def test_prompt_includes_metadata_and_constraints(self):
        from routers.api import _build_auto_character_memory_prompt

        prompt = _build_auto_character_memory_prompt(
            {
                "title": "カフェ・ロマーナへようこそ",
                "seiyuu": '["茶介"]',
                "description": "小霧幸人の恋の物語。",
                "description_en": "The love story of Kogiri Yukito.",
            }
        )
        self.assertIn("カフェ・ロマーナへようこそ", prompt)
        self.assertIn("茶介", prompt)
        self.assertIn("Kogiri Yukito", prompt)
        self.assertIn("Do NOT invent", prompt)
        self.assertIn('{"character_memory"', prompt.replace("\\\"", "\""))

    def test_coerce_character_memory_shapes(self):
        from routers.api import _coerce_character_memory_text

        self.assertEqual(_coerce_character_memory_text({"character_memory": "a\nb"}), "a\nb")
        self.assertEqual(_coerce_character_memory_text({"character_memory": ["a", "b"]}), "a\nb")
        self.assertEqual(_coerce_character_memory_text(["a"]), "a")
        self.assertEqual(_coerce_character_memory_text(None), "")


class AutopilotGlossaryStageTests(unittest.TestCase):
    def test_stage_registered_in_order(self):
        from pipeline.autopilot_job import STAGE_NAMES, stage_label

        self.assertIn("glossary_build", STAGE_NAMES)
        # Must run after metadata translation (it mines title_en/description_en)
        # and before transcription (hotwords read the item glossary).
        self.assertLess(
            STAGE_NAMES.index("metadata_translate"), STAGE_NAMES.index("glossary_build")
        )
        self.assertLess(STAGE_NAMES.index("glossary_build"), STAGE_NAMES.index("transcribe"))
        self.assertEqual(stage_label("glossary_build"), "Building glossary")


class FeedbackGlossaryTests(unittest.TestCase):
    def test_select_prefers_name_bearing_segments_and_caps(self):
        from pipeline.translation_job import _select_feedback_pairs

        cleaned = [
            {"segment_index": 0, "source_text": "おはよう"},
            {"segment_index": 1, "source_text": "ロマーナへようこそ"},  # katakana run
            {"segment_index": 2, "source_text": "Hello there"},  # no CJK -> dropped
            {"segment_index": 3, "source_text": "幸人くん、こっち"},  # honorific
        ]
        tmap = {
            0: {"text": "Morning"},
            1: {"text": "Welcome to Romana"},
            2: {"text": "Hello there"},
            3: {"text": "Yukito, over here"},
        }
        pairs = _select_feedback_pairs(cleaned, tmap)
        self.assertEqual(len(pairs), 3)
        # Hinted segments come first regardless of original order.
        self.assertEqual(pairs[0]["ja"], "ロマーナへようこそ")
        self.assertEqual(pairs[1]["ja"], "幸人くん、こっち")
        self.assertEqual(pairs[2]["ja"], "おはよう")

    def test_select_respects_char_budget(self):
        from pipeline.translation_job import _select_feedback_pairs

        cleaned = [{"segment_index": i, "source_text": "あ" * 100} for i in range(100)]
        tmap = {i: {"text": "x" * 100} for i in range(100)}
        pairs = _select_feedback_pairs(cleaned, tmap, max_chars=1000)
        self.assertEqual(len(pairs), 5)  # 200 chars per pair

    def test_select_skips_untranslated_segments(self):
        from pipeline.translation_job import _select_feedback_pairs

        cleaned = [{"segment_index": 0, "source_text": "おはよう"}]
        self.assertEqual(_select_feedback_pairs(cleaned, {}), [])

    def test_prompt_contains_pairs_and_contract(self):
        from pipeline.translation_job import _build_feedback_glossary_prompt

        prompt = _build_feedback_glossary_prompt([{"ja": "ロマーナ", "en": "Romana"}])
        self.assertIn("ロマーナ", prompt)
        self.assertIn("Romana", prompt)
        self.assertIn("日本語=English", prompt)
        self.assertIn("proper-noun", prompt)
        self.assertIn('{"glossary"', prompt.replace("\\\"", "\""))

    def test_coerce_validates_lines(self):
        from pipeline.translation_job import _coerce_feedback_glossary

        raw = {
            "glossary": "小霧幸人=Kogiri Yukito\n"
            "no equals sign here\n"
            "ascii=only left side\n"
            "=missing left\n"
            "茶介=Chasuke"
        }
        self.assertEqual(
            _coerce_feedback_glossary(raw), "小霧幸人=Kogiri Yukito\n茶介=Chasuke"
        )

    def test_coerce_accepts_list_and_none(self):
        from pipeline.translation_job import _coerce_feedback_glossary

        self.assertEqual(_coerce_feedback_glossary({"glossary": ["茶介=Chasuke"]}), "茶介=Chasuke")
        self.assertEqual(_coerce_feedback_glossary(None), "")


class LooksTranslatedTests(unittest.TestCase):
    """Echo detection: a 'translation' that is still mostly Japanese must not
    count as translated — it used to poison the autopilot's skip check."""

    def test_real_english_passes(self):
        from text_cleaning import looks_translated_to_english

        self.assertTrue(looks_translated_to_english("Wet Love Oni Tale (Nurekoi Kitan)"))
        # JP proper nouns inside English text are fine.
        self.assertTrue(looks_translated_to_english("The tale of the 鬼 and the office lady"))

    def test_japanese_echo_fails(self):
        from text_cleaning import looks_translated_to_english

        self.assertFalse(looks_translated_to_english("【恐怖、高揚】濡恋鬼譚『骨の髄まで……』"))
        self.assertFalse(looks_translated_to_english(""))
        self.assertFalse(looks_translated_to_english("   "))

    def test_autopilot_skip_check_uses_it(self):
        import inspect
        import pipeline.autopilot_job as ap

        src = inspect.getsource(ap._execute_autopilot)
        self.assertIn("looks_translated_to_english", src)


class GlossaryFeedbackFlagTests(unittest.TestCase):
    """glossary_feedback is default-on: only an explicit opt-out is stored in
    job metadata, so old queued jobs keep working unchanged."""

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

    def test_default_on_not_stored(self):
        from pipeline.service import queue_translation

        job_id = asyncio.run(queue_translation(item_id=1, track_id=2, transcript_run_id=3))
        meta = asyncio.run(database.get_job(job_id))["metadata_json"]
        self.assertNotIn("glossary_feedback", meta)

    def test_opt_out_stored(self):
        from pipeline.service import queue_translation

        job_id = asyncio.run(
            queue_translation(item_id=1, track_id=2, transcript_run_id=3, glossary_feedback=False)
        )
        meta = asyncio.run(database.get_job(job_id))["metadata_json"]
        self.assertIs(meta["glossary_feedback"], False)


if __name__ == "__main__":
    unittest.main()
