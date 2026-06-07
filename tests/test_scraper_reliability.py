import unittest
from unittest.mock import AsyncMock, patch

import httpx

import scraper


class FakeClient:
    def __init__(self, responses):
        self._responses = responses
        self._index = 0

    async def get(self, *args, **kwargs):
        item = self._responses[self._index]
        self._index += 1
        if isinstance(item, Exception):
            raise item
        return item


class ScraperReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_with_retry_recovers_from_timeout(self):
        req = httpx.Request("GET", "https://example.com")
        client = FakeClient([
            httpx.TimeoutException("timeout"),
            httpx.Response(200, request=req, json={"ok": True}),
        ])

        resp, reason = await scraper._request_with_retry(client, "https://example.com")

        self.assertIsNotNone(resp)
        self.assertIsNone(reason)
        self.assertEqual(resp.status_code, 200)

    async def test_request_with_retry_returns_rate_limited(self):
        req = httpx.Request("GET", "https://example.com")
        client = FakeClient([
            httpx.Response(429, request=req),
            httpx.Response(429, request=req),
            httpx.Response(429, request=req),
            httpx.Response(429, request=req),
        ])

        resp, reason = await scraper._request_with_retry(client, "https://example.com")

        self.assertIsNone(resp)
        self.assertEqual(reason, "rate_limited")

    async def test_fetch_all_metadata_tracks_summary(self):
        scraper.scrape_progress.update({
            "running": False,
            "total": 0,
            "completed": 0,
            "current": None,
            "errors": [],
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "error_summary": {},
            "started_at": None,
            "finished_at": None,
        })

        async def fake_fetch(_client, code, wayback=True):
            if code == "RJ00000001":
                return {"title": "OK"}, None
            return None, "rate_limited"

        with patch("scraper.fetch_metadata_for_code", side_effect=fake_fetch), \
             patch("database.update_item_metadata", AsyncMock()):
            result = await scraper.fetch_all_metadata(["RJ00000001", "RJ00000002"], force=True)

        self.assertFalse(result["running"])
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["completed"], 2)
        self.assertEqual(result["success"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["error_summary"].get("rate_limited"), 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("started_at", result)
        self.assertIn("finished_at", result)


if __name__ == "__main__":
    unittest.main()
