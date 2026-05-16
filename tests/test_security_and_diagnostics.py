import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import auth as auth_module
from routers import api as api_router
from routers import scan as scan_router


class ApiKeySecurityTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(api_router.router)
        self.app.include_router(scan_router.router)

    def test_mutating_endpoint_requires_api_key_when_enabled(self):
        with patch.object(auth_module, "API_KEY", "secret-key"):
            with TestClient(self.app) as client:
                unauthorized = client.post("/api/scan/pause")
                authorized = client.post("/api/scan/pause", headers={"X-API-Key": "secret-key"})

        self.assertEqual(unauthorized.status_code, 401)
        self.assertIn("Unauthorized", unauthorized.json()["detail"])
        self.assertEqual(authorized.status_code, 200)
        self.assertEqual(authorized.json()["status"], "idle")

    def test_read_endpoint_stays_open_when_api_key_enabled(self):
        payload = {"items": [], "total": 0, "limit": 500, "offset": 0}
        with patch.object(auth_module, "API_KEY", "secret-key"), \
             patch.object(api_router.db, "get_all_items", AsyncMock(return_value=payload)):
            with TestClient(self.app) as client:
                resp = client.get("/api/items")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), payload)


class DiagnosticsStatusTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(scan_router.router)

    def test_scan_status_includes_job_and_recent_events(self):
        latest_job = {
            "id": 101,
            "status": "running",
            "paused": 0,
            "stopping": 0,
            "result_json": None,
            "error": None,
            "current": "example.rar",
            "total_files": 20,
            "processed_files": 5,
            "matched": 4,
            "unmatched": 1,
            "started_at": "2026-02-12T00:00:00Z",
            "finished_at": None,
        }
        events = [
            {"id": 1, "job_id": 101, "level": "info", "message": "Scan started", "data": {}, "created_at": "2026-02-12T00:00:00Z"}
        ]

        with patch.object(scan_router.db, "get_latest_job", AsyncMock(return_value=latest_job)), \
             patch.object(scan_router.db, "get_job_events", AsyncMock(return_value=events)):
            with TestClient(self.app) as client:
                resp = client.get("/api/scan/status")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["job_id"], 101)
        self.assertEqual(body["status"], "running")
        self.assertEqual(body["current"], "example.rar")
        self.assertEqual(body["recent_events"], events)
        self.assertEqual(body["percent"], 25)

    def test_fetch_status_includes_job_and_recent_events(self):
        latest_job = {
            "id": 202,
            "status": "completed",
            "paused": 0,
            "stopping": 0,
            "stopped": 1,
            "total": 3,
            "completed": 3,
            "current": None,
            "errors_json": [],
            "success": 3,
            "failed": 0,
            "skipped": 0,
            "error_summary_json": {},
            "started_at": "2026-02-12T00:00:00Z",
            "finished_at": "2026-02-12T00:03:00Z",
        }
        events = [
            {"id": 9, "job_id": 202, "level": "info", "message": "Metadata fetch finished", "data": {}, "created_at": "2026-02-12T00:03:00Z"}
        ]

        with patch.object(scan_router.db, "get_latest_job", AsyncMock(return_value=latest_job)), \
             patch.object(scan_router.db, "get_job_events", AsyncMock(return_value=events)):
            with TestClient(self.app) as client:
                resp = client.get("/api/fetch-metadata/status")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["job_id"], 202)
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["total"], 3)
        self.assertEqual(body["completed"], 3)
        self.assertEqual(body["recent_events"], events)


if __name__ == "__main__":
    unittest.main()
