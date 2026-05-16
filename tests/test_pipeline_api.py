import unittest
import importlib
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

pipeline_router = importlib.import_module("pipeline.router")


class PipelineApiTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(pipeline_router.router)
        self.pipeline_enabled_patcher = patch.object(
            pipeline_router.db,
            "get_pipeline_enabled",
            AsyncMock(return_value=True),
        )
        self.pipeline_enabled_patcher.start()

    def tearDown(self):
        self.pipeline_enabled_patcher.stop()

    def test_status_reports_on_demand_policy(self):
        with TestClient(self.app) as client:
            resp = client.get("/api/pipeline/status")

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["enabled"], True)
        self.assertEqual(payload["extraction_mode"], "on_demand")
        self.assertEqual(payload["auto_extract_on_scan"], False)
        self.assertIn("zip", payload.get("archive_support", []))

    def test_queue_item_extraction_requires_existing_item(self):
        with patch.object(pipeline_router.db, "get_item", AsyncMock(return_value=None)):
            with TestClient(self.app) as client:
                resp = client.post("/api/pipeline/items/10/extract", json={"force": False})

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["detail"], "Item not found")

    def test_queue_item_extraction_rejected_when_pipeline_disabled(self):
        with patch.object(pipeline_router.db, "get_pipeline_enabled", AsyncMock(return_value=False)):
            with TestClient(self.app) as client:
                resp = client.post("/api/pipeline/items/10/extract", json={"force": False})

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["detail"], "Pipeline is disabled")

    def test_queue_item_extraction_creates_on_demand_job(self):
        with patch.object(pipeline_router.db, "get_item", AsyncMock(return_value={"id": 10})), \
             patch.object(pipeline_router, "queue_extraction", AsyncMock(return_value=77)), \
             patch.object(pipeline_router, "run_extraction_job", AsyncMock()):
            with TestClient(self.app) as client:
                resp = client.post("/api/pipeline/items/10/extract", json={"force": True})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "queued")
        self.assertEqual(resp.json()["job_id"], 77)
        self.assertTrue(resp.json()["force"])
        self.assertEqual(resp.json()["mode"], "on_demand")

    def test_list_item_tracks(self):
        with patch.object(pipeline_router.db, "get_item", AsyncMock(return_value={"id": 12})), \
             patch.object(pipeline_router.db, "get_pipeline_tracks", AsyncMock(return_value=[{"id": 1, "track_path": "x.mp3"}])):
            with TestClient(self.app) as client:
                resp = client.get("/api/pipeline/items/12/tracks")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["total"], 1)

    def test_item_extraction_status_idle(self):
        with patch.object(pipeline_router.db, "get_item", AsyncMock(return_value={"id": 11})), \
             patch.object(pipeline_router.db, "get_latest_job_for_item", AsyncMock(return_value=None)):
            with TestClient(self.app) as client:
                resp = client.get("/api/pipeline/items/11/extract/status")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "idle")
        self.assertIsNone(resp.json()["job"])

    def test_item_extraction_status_returns_latest_job(self):
        latest = {"id": 5, "status": "queued", "metadata_json": {"item_id": 12}}
        with patch.object(pipeline_router.db, "get_item", AsyncMock(return_value={"id": 12})), \
             patch.object(pipeline_router.db, "get_latest_job_for_item", AsyncMock(return_value=latest)):
            with TestClient(self.app) as client:
                resp = client.get("/api/pipeline/items/12/extract/status")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "queued")
        self.assertEqual(resp.json()["job"]["id"], 5)

    def test_create_and_list_transcript_runs(self):
        created_run = {"id": 91, "track_id": 7, "language": "ja"}
        with patch.object(pipeline_router.db, "get_pipeline_track", AsyncMock(return_value={"id": 7})), \
             patch.object(pipeline_router.db, "create_transcript_run", AsyncMock(return_value=91)), \
             patch.object(pipeline_router.db, "set_track_active_transcript", AsyncMock(return_value=True)), \
             patch.object(pipeline_router.db, "get_transcript_run", AsyncMock(return_value=created_run)), \
             patch.object(pipeline_router.db, "list_transcript_runs", AsyncMock(return_value=[created_run])), \
             patch.object(pipeline_router.db, "get_track_active_outputs", AsyncMock(return_value={"track_id": 7, "active_transcript_run_id": 91, "active_translation_run_id": None, "active_translation_target_language": None})):
            with TestClient(self.app) as client:
                create_resp = client.post(
                    "/api/pipeline/tracks/7/transcripts",
                    json={
                        "language": "ja",
                        "source": "manual",
                        "segments": [
                            {"segment_index": 0, "start_seconds": 0, "end_seconds": 1.2, "text": "a"}
                        ],
                    },
                )
                list_resp = client.get("/api/pipeline/tracks/7/transcripts")

        self.assertEqual(create_resp.status_code, 200)
        self.assertEqual(create_resp.json()["run"]["id"], 91)
        self.assertEqual(list_resp.status_code, 200)
        self.assertEqual(len(list_resp.json()["runs"]), 1)

    def test_create_translation_requires_transcript_for_track(self):
        with patch.object(pipeline_router.db, "get_pipeline_track", AsyncMock(return_value={"id": 7})), \
             patch.object(pipeline_router.db, "get_transcript_run", AsyncMock(return_value=None)):
            with TestClient(self.app) as client:
                resp = client.post(
                    "/api/pipeline/tracks/7/translations",
                    json={
                        "transcript_run_id": 88,
                        "target_language": "en",
                        "segments": [{"segment_index": 0, "text": "hello"}],
                    },
                )

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["detail"], "Transcript run not found for this track")

    def test_set_active_translation(self):
        with patch.object(pipeline_router.db, "get_pipeline_track", AsyncMock(return_value={"id": 7})), \
             patch.object(pipeline_router.db, "set_track_active_translation", AsyncMock(return_value=True)), \
             patch.object(pipeline_router.db, "get_translation_run", AsyncMock(return_value={"id": 12, "target_language": "en"})):
            with TestClient(self.app) as client:
                resp = client.put("/api/pipeline/tracks/7/active-translation/12")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["active_translation_run_id"], 12)

    def test_toggle_pipeline_enabled(self):
        with patch.object(pipeline_router.db, "set_pipeline_enabled", AsyncMock(return_value=False)):
            with TestClient(self.app) as client:
                resp = client.put("/api/pipeline/enabled", json={"enabled": False})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["enabled"], False)

    def test_queue_item_transcription_filters_selected_track_ids(self):
        tracks = [{"id": 1, "track_path": "a.mp3"}, {"id": 2, "track_path": "b.mp3"}]
        with patch.object(pipeline_router.db, "get_item", AsyncMock(return_value={"id": 12})), \
             patch.object(pipeline_router.db, "get_pipeline_tracks", AsyncMock(return_value=tracks)), \
             patch.object(pipeline_router, "_whisper_runtime_ready", return_value=True), \
             patch.object(pipeline_router, "queue_transcription", AsyncMock(return_value=321)) as mock_queue, \
             patch.object(pipeline_router, "run_transcription_job", AsyncMock()):
            with TestClient(self.app) as client:
                resp = client.post(
                    "/api/pipeline/items/12/auto-transcribe",
                    json={"language": "ja", "model": "small", "track_ids": [2]},
                )

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["tracks_queued"], 1)
        self.assertEqual(payload["job_id"], 321)
        mock_queue.assert_awaited_once_with(
            item_id=12,
            language="ja",
            model="small",
            force=False,
            track_ids=[2],
        )

    def test_queue_item_transcription_rejects_when_ffmpeg_missing(self):
        with patch.object(pipeline_router.db, "get_item", AsyncMock(return_value={"id": 12})), \
             patch.object(pipeline_router.db, "get_pipeline_tracks", AsyncMock(return_value=[{"id": 1, "track_path": "a.mp3"}])), \
             patch.object(pipeline_router, "_whisper_runtime_ready", return_value=False):
            with TestClient(self.app) as client:
                resp = client.post(
                    "/api/pipeline/items/12/auto-transcribe",
                    json={"language": "ja", "model": "small", "track_ids": [1]},
                )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("ffmpeg", resp.json().get("detail", "").lower())

    def test_get_transcript_run_includes_clean_source(self):
        segments = [
            {"id": 1, "segment_index": 0, "start_seconds": 0, "end_seconds": 1, "text": "00:00:01,000 --> 00:00:02,000"},
            {"id": 2, "segment_index": 1, "start_seconds": 1, "end_seconds": 2, "text": "<i>MIZUKI: こんにちは</i>"},
            {"id": 3, "segment_index": 2, "start_seconds": 2, "end_seconds": 3, "text": "[BGM]"},
            {"id": 4, "segment_index": 3, "start_seconds": 3, "end_seconds": 4, "text": "ありがとう"},
        ]
        with patch.object(pipeline_router.db, "get_pipeline_track", AsyncMock(return_value={"id": 7})), \
             patch.object(pipeline_router.db, "get_transcript_run", AsyncMock(return_value={"id": 99, "track_id": 7})), \
             patch.object(pipeline_router.db, "get_transcript_segments", AsyncMock(return_value=segments)):
            with TestClient(self.app) as client:
                resp = client.get("/api/pipeline/tracks/7/transcripts/99")

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertIn("clean_source", payload)
        self.assertEqual(payload["clean_source"]["line_count"], 2)
        self.assertEqual(payload["clean_source"]["text"], "こんにちは\nありがとう")

    def test_queue_track_auto_translation(self):
        with patch.object(pipeline_router.db, "get_pipeline_track", AsyncMock(return_value={"id": 7, "item_id": 425})), \
             patch.object(pipeline_router.db, "get_track_active_outputs", AsyncMock(return_value={"active_transcript_run_id": 33})), \
             patch.object(pipeline_router.db, "get_transcript_run", AsyncMock(return_value={"id": 33, "track_id": 7})), \
             patch.object(pipeline_router.db, "get_transcript_segments", AsyncMock(return_value=[{"segment_index": 0, "text": "a"}])), \
             patch.object(pipeline_router.db, "get_runtime_gemini_model", AsyncMock(return_value="gemini-2.0-flash")), \
             patch.object(pipeline_router, "queue_translation", AsyncMock(return_value=501)), \
             patch.object(pipeline_router, "run_translation_job", AsyncMock()):
            with TestClient(self.app) as client:
                resp = client.post(
                    "/api/pipeline/tracks/7/auto-translate",
                    json={"target_language": "en", "provider": "gemini", "max_tokens_per_chunk": 900, "max_lines_per_chunk": 20},
                )

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["job_id"], 501)
        self.assertEqual(payload["transcript_run_id"], 33)

    def test_queue_track_auto_translation_passes_glossary_and_character_memory(self):
        with patch.object(pipeline_router.db, "get_pipeline_track", AsyncMock(return_value={"id": 7, "item_id": 425})), \
             patch.object(pipeline_router.db, "get_track_active_outputs", AsyncMock(return_value={"active_transcript_run_id": 33})), \
             patch.object(pipeline_router.db, "get_transcript_run", AsyncMock(return_value={"id": 33, "track_id": 7})), \
             patch.object(pipeline_router.db, "get_transcript_segments", AsyncMock(return_value=[{"segment_index": 0, "text": "a"}])), \
             patch.object(pipeline_router.db, "get_runtime_gemini_model", AsyncMock(return_value="gemini-2.0-flash")), \
             patch.object(pipeline_router, "queue_translation", AsyncMock(return_value=502)) as mock_queue, \
             patch.object(pipeline_router, "run_translation_job", AsyncMock()):
            with TestClient(self.app) as client:
                resp = client.post(
                    "/api/pipeline/tracks/7/auto-translate",
                    json={
                        "transcript_run_id": 33,
                        "target_language": "en",
                        "provider": "gemini",
                        "glossary": "DLsite=DLsite",
                        "character_memory": "Mizuki speaks politely.",
                    },
                )

        self.assertEqual(resp.status_code, 200)
        mock_queue.assert_awaited_once()
        kwargs = mock_queue.await_args.kwargs
        self.assertEqual(kwargs["glossary"], "DLsite=DLsite")
        self.assertEqual(kwargs["character_memory"], "Mizuki speaks politely.")

    def test_get_pipeline_jobs_filters_pipeline_only(self):
        rows = [
            {"id": 1, "job_type": "scan", "status": "completed"},
            {"id": 2, "job_type": "pipeline_transcribe", "status": "completed"},
            {"id": 3, "job_type": "pipeline_translate", "status": "running"},
        ]
        with patch.object(pipeline_router.db, "get_recent_jobs", AsyncMock(return_value=rows)):
            with TestClient(self.app) as client:
                resp = client.get("/api/pipeline/jobs?limit=20")

        self.assertEqual(resp.status_code, 200)
        jobs = resp.json()["jobs"]
        self.assertEqual([j["id"] for j in jobs], [2, 3])

    def test_get_pipeline_job_events(self):
        job = {"id": 99, "job_type": "pipeline_translate", "status": "running"}
        events = [{"id": 1, "job_id": 99, "level": "info", "message": "chunk", "data": {}, "created_at": "2026-01-01T00:00:00"}]
        with patch.object(pipeline_router.db, "get_job", AsyncMock(return_value=job)), \
             patch.object(pipeline_router.db, "get_job_events", AsyncMock(return_value=events)):
            with TestClient(self.app) as client:
                resp = client.get("/api/pipeline/jobs/99/events?limit=80")

        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["job_id"], 99)
        self.assertEqual(len(payload["events"]), 1)

    def test_pause_resume_stop_pipeline_job(self):
        with patch.object(pipeline_router.db, "get_job", AsyncMock(return_value={"id": 42, "job_type": "pipeline_translate", "status": "running"})), \
             patch.object(pipeline_router.db, "update_job", AsyncMock()) as mock_update, \
             patch.object(pipeline_router.db, "append_job_event", AsyncMock()):
            with TestClient(self.app) as client:
                pause_resp = client.post("/api/pipeline/jobs/42/pause")

        self.assertEqual(pause_resp.status_code, 200)
        self.assertEqual(pause_resp.json()["status"], "paused")
        mock_update.assert_awaited_once()

        with patch.object(pipeline_router.db, "get_job", AsyncMock(return_value={"id": 42, "job_type": "pipeline_translate", "status": "paused"})), \
             patch.object(pipeline_router.db, "update_job", AsyncMock()) as mock_update, \
             patch.object(pipeline_router.db, "append_job_event", AsyncMock()):
            with TestClient(self.app) as client:
                resume_resp = client.post("/api/pipeline/jobs/42/resume")

        self.assertEqual(resume_resp.status_code, 200)
        self.assertEqual(resume_resp.json()["status"], "running")
        mock_update.assert_awaited_once()

        with patch.object(pipeline_router.db, "get_job", AsyncMock(return_value={"id": 42, "job_type": "pipeline_translate", "status": "running"})), \
             patch.object(pipeline_router.db, "update_job", AsyncMock()) as mock_update, \
             patch.object(pipeline_router.db, "append_job_event", AsyncMock()):
            with TestClient(self.app) as client:
                stop_resp = client.post("/api/pipeline/jobs/42/stop")

        self.assertEqual(stop_resp.status_code, 200)
        self.assertEqual(stop_resp.json()["status"], "stopping")
        mock_update.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
