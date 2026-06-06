"""API tests for /api/metadata — fetch-url/search with mocked sources, and
apply against a real temp database (full migration chain)."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiosqlite
from fastapi import FastAPI
from fastapi.testclient import TestClient

import database
import metadata_sources
from routers import metadata as metadata_router

NOW = "2026-01-01T00:00:00Z"

SAMPLE_META = {
    "source": "chil_chil",
    "source_url": "https://www.chil-chil.net/goodsDetail/goods_id/23936/",
    "title": "どうしても触れたくない",
    "title_en": None,
    "release_date": "2009-03-27",
    "seiyuu": ["石川英郎", "野島健児"],
    "description": "新しい職場に初めて出社した日…",
    "cover_url": "https://img.chil-chil.net/goods_img/XL/00023936_XL.jpg",
    "price": None,
    "jan": "4961524410477",
    "catalog_number": None,
    "maker": "ムービック（CD)",
    "series": "どうしても触れたくない",
    "extra": {},
}


def make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(metadata_router.router)
    return app


class FetchSearchTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()

    def test_sources_listing(self):
        with TestClient(self.app) as client:
            resp = client.get("/api/metadata/sources")
        self.assertEqual(resp.status_code, 200)
        names = {s["name"] for s in resp.json()["sources"]}
        self.assertEqual(names, {"gamers", "chil_chil", "rejet"})

    def test_fetch_url_dispatch_and_preview(self):
        source = metadata_sources.get_source("chil_chil")
        with patch.object(source, "fetch_by_url", AsyncMock(return_value=SAMPLE_META)):
            with TestClient(self.app) as client:
                resp = client.post(
                    "/api/metadata/fetch-url",
                    json={"url": "https://www.chil-chil.net/goodsDetail/goods_id/23936/"},
                )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["metadata"]["title"], "どうしても触れたくない")

    def test_fetch_url_unsupported(self):
        with TestClient(self.app) as client:
            resp = client.post("/api/metadata/fetch-url", json={"url": "https://example.com/x"})
        self.assertEqual(resp.status_code, 400)

    def test_search_all_sources_with_partial_failure(self):
        gamers = metadata_sources.get_source("gamers")
        chilchil = metadata_sources.get_source("chil_chil")
        rejet = metadata_sources.get_source("rejet")
        hit = {"source": "chil_chil", "title": "t", "url": "u", "thumbnail": None,
               "release_date": None, "price": None, "category": "CD"}
        with patch.object(gamers, "search", AsyncMock(side_effect=RuntimeError("boom"))), \
             patch.object(chilchil, "search", AsyncMock(return_value=[hit])), \
             patch.object(rejet, "search", AsyncMock(return_value=[])):
            with TestClient(self.app) as client:
                resp = client.post("/api/metadata/search", json={"query": "xyz"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body["results"]), 1)
        self.assertEqual(len(body["errors"]), 1)
        self.assertIn("Gamers", body["errors"][0])

    def test_search_single_source(self):
        gamers = metadata_sources.get_source("gamers")
        with patch.object(gamers, "search", AsyncMock(return_value=[])) as mock_search:
            with TestClient(self.app) as client:
                resp = client.post(
                    "/api/metadata/search", json={"query": "abc", "source": "gamers"}
                )
        self.assertEqual(resp.status_code, 200)
        mock_search.assert_awaited_once()


class ApplyTests(unittest.TestCase):
    """Apply endpoints against a real temp DB built by the migration chain."""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls._old_db_path = database.DB_PATH
        database.DB_PATH = Path(cls._tmpdir.name) / "test.db"
        asyncio.run(database.init_db())
        asyncio.run(cls._seed())

    @classmethod
    def tearDownClass(cls):
        database.DB_PATH = cls._old_db_path
        cls._tmpdir.cleanup()

    @classmethod
    async def _seed(cls):
        async with aiosqlite.connect(database.DB_PATH) as conn:
            await conn.execute(
                """INSERT INTO items (product_code, title, kind, is_manual, notes,
                                      created_at, updated_at)
                   VALUES ('MAN-TEST01', 'placeholder', 'drama_cd', 1, '', ?, ?)""",
                (NOW, NOW),
            )
            await conn.execute(
                """INSERT INTO tokutens (kind, title, shop, notes, created_at, updated_at)
                   VALUES ('audio', '[New Tokuten]', 'other', '', ?, ?)""",
                (NOW, NOW),
            )
            cur = await conn.execute("SELECT id FROM tokutens WHERE title = '[New Tokuten]'")
            tokuten_id = (await cur.fetchone())[0]
            await conn.execute(
                """INSERT INTO items (product_code, title, kind, tokuten_id, is_manual,
                                      created_at, updated_at)
                   VALUES ('TKT-TEST01', '[New Tokuten]', 'tokuten_audio', ?, 1, ?, ?)""",
                (tokuten_id, NOW, NOW),
            )
            await conn.commit()
            cls.tokuten_id = tokuten_id
            cur = await conn.execute(
                "SELECT id FROM items WHERE product_code = 'MAN-TEST01'"
            )
            cls.item_id = (await cur.fetchone())[0]

    def setUp(self):
        self.app = make_app()

    def test_apply_to_item(self):
        with patch.object(
            metadata_router, "download_cover",
            AsyncMock(return_value=("data/covers/MAN-TEST01.jpg", None)),
        ):
            with TestClient(self.app) as client:
                resp = client.post(
                    "/api/metadata/apply",
                    json={
                        "target": "item",
                        "target_id": self.item_id,
                        "metadata": SAMPLE_META,
                        "fields": ["title", "release_date", "seiyuu",
                                   "description", "cover", "source_note"],
                    },
                )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        item = body["item"]
        self.assertEqual(item["title"], "どうしても触れたくない")
        self.assertEqual(item["release_date"], "2009-03-27")
        self.assertIn("石川英郎", item["seiyuu"])
        self.assertIn("JAN 4961524410477", item["notes"])
        self.assertIn("fetched ", item["notes"])
        self.assertEqual(item["cover_local"], "data/covers/MAN-TEST01.jpg")
        self.assertIn("cover", body["applied"])

    def test_apply_to_tokuten_mirrors_paired_item(self):
        with patch.object(
            metadata_router, "download_cover",
            AsyncMock(return_value=("data/covers/TKT-TEST01.jpg", None)),
        ):
            with TestClient(self.app) as client:
                resp = client.post(
                    "/api/metadata/apply",
                    json={
                        "target": "tokuten",
                        "target_id": self.tokuten_id,
                        "metadata": SAMPLE_META,
                        "fields": ["title", "release_date", "seiyuu", "description",
                                   "shop", "source_url", "cover", "source_note"],
                    },
                )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        tok = body["tokuten"]
        self.assertEqual(tok["title"], "どうしても触れたくない")
        self.assertEqual(tok["shop"], "chil_chil")
        self.assertEqual(tok["source_url"], SAMPLE_META["source_url"])
        self.assertIn("石川英郎", tok["seiyuu"])
        self.assertEqual(tok["cover_local"], "data/covers/TKT-TEST01.jpg")

        async def _check_mirror():
            async with aiosqlite.connect(database.DB_PATH) as conn:
                conn.row_factory = aiosqlite.Row
                cur = await conn.execute(
                    "SELECT * FROM items WHERE tokuten_id = ?", (self.tokuten_id,)
                )
                return dict(await cur.fetchone())

        mirrored = asyncio.run(_check_mirror())
        self.assertEqual(mirrored["title"], "どうしても触れたくない")
        self.assertEqual(mirrored["release_date"], "2009-03-27")
        self.assertIn("石川英郎", mirrored["seiyuu"])
        self.assertEqual(mirrored["cover_local"], "data/covers/TKT-TEST01.jpg")

    def test_apply_unknown_target(self):
        with TestClient(self.app) as client:
            resp = client.post(
                "/api/metadata/apply",
                json={"target": "game", "target_id": 1, "metadata": {}, "fields": []},
            )
        self.assertEqual(resp.status_code, 400)

    def test_apply_missing_item_404(self):
        with TestClient(self.app) as client:
            resp = client.post(
                "/api/metadata/apply",
                json={"target": "item", "target_id": 999999,
                      "metadata": SAMPLE_META, "fields": ["title"]},
            )
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
