import unittest
from unittest.mock import AsyncMock

from pipeline.chutes_translator import ChutesTrackTranslator
from pipeline.openrouter_translator import OpenRouterTrackTranslator
from pipeline.translation_job import _align_chunk_output_rows


class TranslationAlignmentTests(unittest.TestCase):
    def test_aligns_indexless_rows_positionally(self):
        expected = [1, 2, 3]
        out_rows = [{"text": "a"}, {"text": "b"}, {"text": "c"}]
        out_map, mode = _align_chunk_output_rows(expected, out_rows)
        self.assertEqual(mode, "positional")
        self.assertEqual(out_map[1]["text"], "a")
        self.assertEqual(out_map[2]["text"], "b")
        self.assertEqual(out_map[3]["text"], "c")

    def test_aligns_mixed_direct_and_indexless_rows(self):
        expected = [1, 2, 3]
        out_rows = [
            {"segment_index": 1, "text": "one"},
            {"text": "two"},
            {"text": "three"},
            {"text": "extra"},
        ]
        out_map, mode = _align_chunk_output_rows(expected, out_rows)
        self.assertEqual(mode, "mixed_positional")
        self.assertEqual(out_map[1]["text"], "one")
        self.assertEqual(out_map[2]["text"], "two")
        self.assertEqual(out_map[3]["text"], "three")


class TranslatorNormalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_openrouter_translate_chunk_keeps_indexless_rows(self):
        translator = OpenRouterTrackTranslator(api_key="k", model="m")
        translator._send_text = AsyncMock(return_value='[{"text":"hello"},{"text":"world"}]')
        rows = await translator.translate_chunk(
            target_language="en",
            description_context="",
            chunk_segments=[{"segment_index": 1, "source_text": "a"}, {"segment_index": 2, "source_text": "b"}],
            prior_context=[],
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["text"], "hello")
        self.assertNotIn("segment_index", rows[0])

    async def test_chutes_translate_chunk_reads_segment_index_alias(self):
        translator = ChutesTrackTranslator(api_key="k", model="m")
        translator._send_text = AsyncMock(return_value='[{"segmentIndex":1,"content":"hello"}]')
        rows = await translator.translate_chunk(
            target_language="en",
            description_context="",
            chunk_segments=[{"segment_index": 1, "source_text": "a"}],
            prior_context=[],
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["segment_index"], 1)
        self.assertEqual(rows[0]["text"], "hello")


if __name__ == "__main__":
    unittest.main()
