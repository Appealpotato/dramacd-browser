import asyncio
import io
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pipeline.extractor as extractor


class PipelineExtractorTests(unittest.TestCase):
    def test_run_extraction_job_indexes_tracks_from_zip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            scan_root = root / "scan"
            scan_root.mkdir(parents=True, exist_ok=True)
            archive_path = scan_root / "RJ12345678-sample.zip"
            wav_buf = io.BytesIO()
            with wave.open(wav_buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(22050)
                wf.writeframes(b"\x00\x00" * 2205)
            with zipfile.ZipFile(archive_path, "w") as zf:
                zf.writestr("disc1/01-intro.mp3", b"fake")
                zf.writestr("disc1/02-main.wav", wav_buf.getvalue())
                zf.writestr("readme.txt", b"ignore")

            updates = []
            captured_tracks = []

            async def fake_update_job(_job_id, **fields):
                updates.append(fields)

            async def fake_replace_tracks(_item_id, tracks):
                captured_tracks.extend(tracks)
                return len(tracks)

            async def _run():
                with patch.object(extractor.db, "get_job", AsyncMock(return_value={"id": 5, "job_type": "pipeline_extract", "metadata_json": {"item_id": 99, "force": False}})), \
                     patch.object(extractor.db, "get_item", AsyncMock(return_value={"id": 99, "product_code": "RJ12345678", "original_code": "RJ12345678", "files": '["RJ12345678-sample.zip"]'})), \
                     patch.object(extractor.db, "get_scan_paths", AsyncMock(return_value=[str(scan_root)])), \
                     patch.object(extractor.db, "update_job", AsyncMock(side_effect=fake_update_job)), \
                     patch.object(extractor.db, "append_job_event", AsyncMock()), \
                     patch.object(extractor.db, "replace_pipeline_tracks_for_item", AsyncMock(side_effect=fake_replace_tracks)), \
                     patch.object(extractor, "PIPELINE_EXTRACT_DIR", root / "extract"):
                    await extractor.run_extraction_job(5)

            asyncio.run(_run())
            self.assertTrue(any(update.get("status") == "completed" for update in updates))
            wav_tracks = [t for t in captured_tracks if str(t.get("track_path", "")).lower().endswith(".wav")]
            self.assertTrue(len(captured_tracks) >= 2)
            self.assertEqual(len(wav_tracks), 1)
            self.assertIsNotNone(wav_tracks[0]["duration_seconds"])
            self.assertIsNotNone(wav_tracks[0]["sample_rate"])
            self.assertIsNotNone(wav_tracks[0]["channels"])
            self.assertEqual(wav_tracks[0]["codec"], "pcm_s16le")

    def test_run_extraction_job_fails_for_rar_without_7z(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            scan_root = root / "scan"
            scan_root.mkdir(parents=True, exist_ok=True)
            archive_path = scan_root / "RJ12345678-sample.rar"
            archive_path.write_bytes(b"not-real-rar")

            updates = []

            async def fake_update_job(_job_id, **fields):
                updates.append(fields)

            async def _run():
                with patch.object(extractor.db, "get_job", AsyncMock(return_value={"id": 6, "job_type": "pipeline_extract", "metadata_json": {"item_id": 99, "force": False}})), \
                     patch.object(extractor.db, "get_item", AsyncMock(return_value={"id": 99, "product_code": "RJ12345678", "original_code": "RJ12345678", "files": '["RJ12345678-sample.rar"]'})), \
                     patch.object(extractor.db, "get_scan_paths", AsyncMock(return_value=[str(scan_root)])), \
                     patch.object(extractor.db, "update_job", AsyncMock(side_effect=fake_update_job)), \
                     patch.object(extractor.db, "append_job_event", AsyncMock()), \
                     patch.object(extractor.db, "replace_pipeline_tracks_for_item", AsyncMock(return_value=0)), \
                     patch.object(extractor, "_find_binary", return_value=None), \
                     patch.object(extractor, "PIPELINE_EXTRACT_DIR", root / "extract"):
                    await extractor.run_extraction_job(6)

            asyncio.run(_run())
            final_updates = [u for u in updates if u.get("status") in {"completed", "failed"}]
            self.assertTrue(final_updates)
            self.assertEqual(final_updates[-1]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
