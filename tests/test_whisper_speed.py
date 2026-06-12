"""Whisper speed rework: warm model cache, ct2-based device detection,
optional word timestamps (stored in segment meta), batched decoding gate."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.transcriber import WhisperTranscriber


class DeviceDetectionTests(unittest.TestCase):
    def test_returns_valid_pair(self):
        device, compute_type = WhisperTranscriber._detect_device()
        self.assertIn(device, {"cuda", "cpu"})
        self.assertIn(compute_type, {"float16", "int8"})

    def test_no_torch_dependency(self):
        # Device detection must ask CTranslate2 (the actual inference engine),
        # not torch — a CPU-only torch build used to silently force CPU.
        import pipeline.transcriber as t

        self.assertNotIn("torch", dir(t))


class BatchingGateTests(unittest.TestCase):
    def test_batched_requires_vad(self):
        t = WhisperTranscriber(vad_filter=False, batch_size=8)
        self.assertFalse(t._uses_batching())

    def test_batched_with_vad(self):
        t = WhisperTranscriber(vad_filter=True, batch_size=8)
        self.assertTrue(t._uses_batching())

    def test_batch_size_one_is_sequential(self):
        t = WhisperTranscriber(vad_filter=True, batch_size=1)
        self.assertFalse(t._uses_batching())

    def test_batch_size_zero_is_sequential(self):
        t = WhisperTranscriber(vad_filter=True, batch_size=0)
        self.assertFalse(t._uses_batching())

    def test_default_batch_size_from_config(self):
        from config import WHISPER_BATCH_SIZE

        t = WhisperTranscriber()
        self.assertEqual(t.batch_size, WHISPER_BATCH_SIZE)


class WordTimestampsTests(unittest.TestCase):
    def test_default_off(self):
        self.assertFalse(WhisperTranscriber().word_timestamps)

    def test_parse_segments_carries_words_into_meta(self):
        output = {
            "segments": [
                {
                    "id": 0,
                    "start": 0.0,
                    "end": 1.5,
                    "text": "おはよう",
                    "words": [{"w": "おはよう", "s": 0.1, "e": 1.4}],
                },
                {"id": 1, "start": 1.5, "end": 3.0, "text": "こんにちは"},
            ]
        }
        segments = WhisperTranscriber.parse_segments(output, deduplicate=False)
        self.assertEqual(segments[0]["meta"], {"words": [{"w": "おはよう", "s": 0.1, "e": 1.4}]})
        self.assertIsNone(segments[1]["meta"])

    def test_dedupe_still_renumbers_with_words(self):
        output = {
            "segments": [
                {"id": 0, "start": 0.0, "end": 1.0, "text": "あ", "words": [{"w": "あ", "s": 0, "e": 1}]},
                {"id": 1, "start": 1.0, "end": 2.0, "text": "あ"},
                {"id": 2, "start": 2.0, "end": 3.0, "text": "い"},
            ]
        }
        segments = WhisperTranscriber.parse_segments(output, deduplicate=True)
        self.assertEqual([s["segment_index"] for s in segments], [0, 1])
        self.assertEqual([s["text"] for s in segments], ["あ", "い"])


class ModelCacheTests(unittest.TestCase):
    def test_cache_is_module_level_single_slot(self):
        import pipeline.transcriber as t

        # The cache must exist at module scope (instances used to each own a
        # model, reloading ~3GB from disk on every job).
        self.assertTrue(hasattr(t, "_get_cached_model"))
        self.assertTrue(hasattr(t, "_model_cache_lock"))


if __name__ == "__main__":
    unittest.main()
