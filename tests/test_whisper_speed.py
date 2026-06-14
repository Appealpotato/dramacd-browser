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


class _Info:
    duration = 10.0
    language = "ja"
    language_probability = 0.99


class _Seg:
    def __init__(self, text="ok"):
        self.id = 0
        self.start = 0.0
        self.end = 1.0
        self.text = text
        self.words = None


class _FakeModel:
    """Stand-in for WhisperModel: records the hotwords each transcribe() got,
    and (optionally) raises the decoder's overflow ValueError when hotwords
    are present — lazily, during generator iteration, like the real engine."""

    def __init__(self, fail_with_hotwords=True, always_fail=False,
                 fail_message="The maximum decoding length must be > 0"):
        self.calls = []
        self._fail_with_hotwords = fail_with_hotwords
        self._always_fail = always_fail
        self._fail_message = fail_message

    def transcribe(self, path, **kwargs):
        hotwords = kwargs.get("hotwords")
        self.calls.append(hotwords)
        msg = self._fail_message

        def gen_fail():
            raise ValueError(msg)
            yield  # pragma: no cover - makes this a generator

        def gen_ok():
            yield _Seg("ok")

        if self._always_fail or (hotwords and self._fail_with_hotwords):
            return gen_fail(), _Info()
        return gen_ok(), _Info()


class HotwordsOverflowRetryTests(unittest.TestCase):
    def _transcriber(self, fake, hotwords="x" * 200):
        t = WhisperTranscriber(hotwords=hotwords)
        t._model = fake  # bypass real model load (_load_model no-ops when set)
        return t

    def test_retries_without_hotwords_on_overflow(self):
        fake = _FakeModel(fail_with_hotwords=True)
        t = self._transcriber(fake)
        result = t._transcribe_with_progress("dummy.wav")
        # First attempt carried hotwords; second dropped them and succeeded.
        self.assertEqual(fake.calls, ["x" * 200, None])
        self.assertEqual(len(result["segments"]), 1)
        self.assertEqual(result["segments"][0]["text"], "ok")

    def test_no_retry_when_hotwords_absent(self):
        # Overflow error but no hotwords to drop → propagate, don't loop.
        fake = _FakeModel(always_fail=True)
        t = self._transcriber(fake, hotwords=None)
        with self.assertRaises(ValueError):
            t._transcribe_with_progress("dummy.wav")
        self.assertEqual(fake.calls, [None])  # exactly one attempt

    def test_unrelated_value_error_not_retried(self):
        fake = _FakeModel(fail_with_hotwords=True, fail_message="some other problem")
        t = self._transcriber(fake)
        with self.assertRaises(ValueError):
            t._transcribe_with_progress("dummy.wav")
        self.assertEqual(fake.calls, ["x" * 200])  # no retry on unrelated error

    def test_success_path_keeps_hotwords(self):
        fake = _FakeModel(fail_with_hotwords=False)
        t = self._transcriber(fake)
        result = t._transcribe_with_progress("dummy.wav")
        self.assertEqual(fake.calls, ["x" * 200])  # one attempt, no retry
        self.assertEqual(len(result["segments"]), 1)


if __name__ == "__main__":
    unittest.main()
