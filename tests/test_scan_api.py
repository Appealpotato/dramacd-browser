import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import scan as scan_router


class ScanApiTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(scan_router.router)
        scan_router.scan_state.update({
            "running": False,
            "paused": False,
            "stopping": False,
            "result": None,
            "error": None,
            "current": None,
            "total_files": 0,
            "processed_files": 0,
            "matched": 0,
            "unmatched": 0,
            "started_at": None,
            "finished_at": None,
        })

    def test_get_scan_paths(self):
        with patch.object(scan_router.db, "get_scan_paths", AsyncMock(return_value=["X:/A", "X:/B"])):
            with TestClient(self.app) as client:
                resp = client.get("/api/scan/paths")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"paths": ["X:/A", "X:/B"]})

    def test_update_scan_paths_success(self):
        with patch.object(scan_router.db, "set_scan_paths", AsyncMock(return_value=["D:/Drama", "D:/Drama/Hirame"])):
            with TestClient(self.app) as client:
                resp = client.put("/api/scan/paths", json={"paths": ["D:/Drama", "D:/Drama/Hirame"]})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "updated")
        self.assertEqual(resp.json()["paths"], ["D:/Drama", "D:/Drama/Hirame"])

    def test_update_scan_paths_validation_error(self):
        with patch.object(scan_router.db, "set_scan_paths", AsyncMock(side_effect=ValueError("At least one scan path is required"))):
            with TestClient(self.app) as client:
                resp = client.put("/api/scan/paths", json={"paths": []})

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"], "At least one scan path is required")

    def test_trigger_scan_respects_recursive_and_paths(self):
        captured = {}

        def fake_scan_folder_with_progress(scan_path=None, scan_paths=None, recursive=True, on_progress=None, pause_event=None, stop_event=None):
            captured["scan_paths"] = scan_paths
            captured["recursive"] = recursive
            if on_progress:
                on_progress({"processed_files": 1, "total_files": 1, "current": "x.rar", "matched": 1, "unmatched": 0})
            return {
                "items": {},
                "unmatched": [],
                "stats": {
                    "total_files": 1,
                    "processed_files": 1,
                    "matched": 1,
                    "unmatched": 0,
                    "unique_codes": 0,
                    "recursive": recursive,
                    "scanned_paths": scan_paths or [],
                    "missing_paths": [],
                    "stopped": False,
                },
            }

        with patch.object(scan_router, "scan_folder_with_progress", side_effect=fake_scan_folder_with_progress), \
             patch.object(scan_router.db, "clear_unmatched_files", AsyncMock()), \
             patch.object(scan_router.db, "get_ignored_codes", AsyncMock(return_value=set())), \
             patch.object(scan_router.db, "upsert_item", AsyncMock()), \
             patch.object(scan_router.db, "add_unmatched_file", AsyncMock()):
            with TestClient(self.app) as client:
                resp = client.post("/api/scan", json={"paths": ["D:/Drama"], "recursive": False})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "started")
        self.assertEqual(captured["scan_paths"], ["D:/Drama"])
        self.assertFalse(captured["recursive"])

    def test_pause_resume_stop_endpoints(self):
        scan_router.scan_state["running"] = True
        scan_router.scan_state["paused"] = False
        scan_router.scan_state["stopping"] = False

        with TestClient(self.app) as client:
            pause_resp = client.post('/api/scan/pause')
            self.assertEqual(pause_resp.status_code, 200)
            self.assertEqual(pause_resp.json()["status"], "paused")

            resume_resp = client.post('/api/scan/resume')
            self.assertEqual(resume_resp.status_code, 200)
            self.assertEqual(resume_resp.json()["status"], "resumed")

            stop_resp = client.post('/api/scan/stop')
            self.assertEqual(stop_resp.status_code, 200)
            self.assertEqual(stop_resp.json()["status"], "stopping")

    def test_scan_skips_ignored_codes(self):
        def fake_scan_folder_with_progress(scan_path=None, scan_paths=None, recursive=True, on_progress=None, pause_event=None, stop_event=None):
            return {
                "items": {
                    "RJ11111111": {
                        "product_code": "RJ11111111",
                        "original_code": "DLJ-11111111",
                        "confidence": "low",
                        "files": ["file1.zip"],
                        "file_count": 1,
                        "total_size": 123,
                        "formats": ["MP3"],
                    }
                },
                "unmatched": [],
                "stats": {
                    "total_files": 1,
                    "processed_files": 1,
                    "matched": 1,
                    "unmatched": 0,
                    "unique_codes": 1,
                    "recursive": recursive,
                    "scanned_paths": scan_paths or [],
                    "missing_paths": [],
                    "stopped": False,
                },
            }

        mock_upsert = AsyncMock()
        with patch.object(scan_router, "scan_folder_with_progress", side_effect=fake_scan_folder_with_progress), \
             patch.object(scan_router.db, "clear_unmatched_files", AsyncMock()), \
             patch.object(scan_router.db, "get_ignored_codes", AsyncMock(return_value={"RJ11111111"})), \
             patch.object(scan_router.db, "upsert_item", mock_upsert), \
             patch.object(scan_router.db, "add_unmatched_file", AsyncMock()):
            with TestClient(self.app) as client:
                resp = client.post("/api/scan", json={"paths": ["D:/Drama"], "recursive": True})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "started")
        self.assertEqual(mock_upsert.await_count, 0)

    def test_fetch_pause_resume_stop_endpoints(self):
        scan_router.scrape_progress.update({"running": True, "paused": False, "stopping": False, "stopped": False})

        with TestClient(self.app) as client:
            pause_resp = client.post('/api/fetch-metadata/pause')
            self.assertEqual(pause_resp.status_code, 200)
            self.assertEqual(pause_resp.json()["status"], "paused")

            resume_resp = client.post('/api/fetch-metadata/resume')
            self.assertEqual(resume_resp.status_code, 200)
            self.assertEqual(resume_resp.json()["status"], "resumed")

            stop_resp = client.post('/api/fetch-metadata/stop')
            self.assertEqual(stop_resp.status_code, 200)
            self.assertEqual(stop_resp.json()["status"], "stopping")


class FetchCandidateTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_fetch_metadata_includes_existing_items_missing_en_fields(self):
        fake_items = {
            "items": [
                {"product_code": "RJ0001", "title": "jp", "title_en": "en", "tags_en": '["a"]'},
                {"product_code": "RJ0002", "title": "jp", "title_en": None, "tags_en": '["a"]'},
                {"product_code": "RJ0003", "title": "jp", "title_en": "en", "tags_en": "[]"},
                {"product_code": "RJ0004", "title": None, "title_en": None, "tags_en": None},
            ]
        }

        with patch.object(scan_router.db, "get_all_items", AsyncMock(return_value=fake_items)), \
             patch.object(scan_router, "fetch_all_metadata", AsyncMock()) as mock_fetch:
            await scan_router.run_fetch_metadata(product_codes=None, force=False)

        args, kwargs = mock_fetch.await_args
        self.assertEqual(args[0], ["RJ0002", "RJ0003", "RJ0004"])


if __name__ == "__main__":
    unittest.main()
