"""API tests for /api/metadata — fetch-url/search with mocked sources, and
apply against a real temp database (full migration chain)."""
import asyncio
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiosqlite
from fastapi import FastAPI
from fastapi.testclient import TestClient

import database
import metadata_sources
from metadata_sources.base import empty_metadata
from routers import api as api_router
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
    app.include_router(api_router.router)
    return app


def _vol_meta(n: int, cv: str) -> dict:
    meta = empty_metadata("rejet", f"https://rejet.jp/works/?s=x#rejet=REC-{850 + n}")
    meta["title"] = f"シリーズCD Vol.{n} CV.{cv}"
    meta["seiyuu"] = [cv]
    meta["release_date"] = f"2019-0{n}-20"
    meta["description"] = "shared blurb"
    meta["catalog_number"] = f"REC-{850 + n}"
    meta["cover_url"] = f"https://example.com/REC-{850 + n}.jpg"
    return meta


class FetchSearchTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()

    def test_sources_listing(self):
        with TestClient(self.app) as client:
            resp = client.get("/api/metadata/sources")
        self.assertEqual(resp.status_code, 200)
        names = {s["name"] for s in resp.json()["sources"]}
        self.assertEqual(names, {
            "dlsite", "gamers", "chil_chil", "rejet",
            "booth", "animate", "stellaworth", "fanza", "melon",
            "digiket", "gyutto",
        })

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
        hit = {"source": "chil_chil", "title": "t", "url": "u", "thumbnail": None,
               "release_date": None, "price": None, "category": "CD"}
        # Patch every registered source so no test ever hits the network,
        # including sources added after this test was written.
        with ExitStack() as stack:
            for source in metadata_sources.SOURCES:
                if source.name == "gamers":
                    mock = AsyncMock(side_effect=RuntimeError("boom"))
                elif source.name == "chil_chil":
                    mock = AsyncMock(return_value=[hit])
                else:
                    mock = AsyncMock(return_value=[])
                stack.enter_context(patch.object(source, "search", mock))
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

    def test_fetch_multi_merges_volumes(self):
        rejet = metadata_sources.get_source("rejet")
        vols = [_vol_meta(1, "A声優"), _vol_meta(2, "B声優")]
        with patch.object(rejet, "fetch_by_url", AsyncMock(side_effect=vols)):
            with TestClient(self.app) as client:
                resp = client.post(
                    "/api/metadata/fetch-multi",
                    json={"urls": [v["source_url"] for v in vols]},
                )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["fetched"], 2)
        meta = body["metadata"]
        self.assertEqual(meta["title"], "シリーズCD")
        self.assertEqual(meta["seiyuu"], ["A声優", "B声優"])
        self.assertEqual(meta["release_date"], "2019-01-20")
        self.assertEqual(len(meta["extra"]["volumes"]), 2)

    def test_fetch_multi_partial_failure_still_merges(self):
        rejet = metadata_sources.get_source("rejet")
        with patch.object(
            rejet, "fetch_by_url",
            AsyncMock(side_effect=[_vol_meta(1, "A声優"), RuntimeError("boom")]),
        ):
            with TestClient(self.app) as client:
                resp = client.post(
                    "/api/metadata/fetch-multi",
                    json={"urls": ["https://rejet.jp/works/?s=x#rejet=REC-851",
                                   "https://rejet.jp/works/?s=x#rejet=REC-852"]},
                )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["fetched"], 1)
        self.assertEqual(len(body["errors"]), 1)

    def test_fetch_multi_rejects_oversize_and_unsupported(self):
        with TestClient(self.app) as client:
            resp = client.post(
                "/api/metadata/fetch-multi",
                json={"urls": ["https://rejet.jp/works/"] * 21},
            )
            self.assertEqual(resp.status_code, 400)
            resp = client.post(
                "/api/metadata/fetch-multi", json={"urls": ["https://example.com/x"]}
            )
            self.assertEqual(resp.status_code, 400)
            resp = client.post("/api/metadata/fetch-multi", json={"urls": []})
            self.assertEqual(resp.status_code, 400)


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
                """INSERT INTO items (product_code, title, kind, is_manual, notes,
                                      seiyuu, created_at, updated_at)
                   VALUES ('MAN-TEST02', 'multi', 'drama_cd', 1, '',
                           '["既存の人"]', ?, ?)""",
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
            cur = await conn.execute(
                "SELECT id FROM items WHERE product_code = 'MAN-TEST02'"
            )
            cls.multi_item_id = (await cur.fetchone())[0]

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

    def test_apply_multi_volume_full_flow(self):
        """Merged multi-volume apply: cast unions with existing names,
        per-volume note lines, extra covers land in the media gallery
        (deduped on re-apply), set-cover promotes, delete removes."""
        from metadata_sources.merge import merge_metadata

        merged = merge_metadata([_vol_meta(1, "A声優"), _vol_meta(2, "B声優")])
        fields = ["title", "release_date", "seiyuu", "description", "cover", "source_note"]
        covers = [
            ("data/covers/MAN-TEST02.jpg", None),       # primary
            ("data/covers/MAN-TEST02_vol1.jpg", None),  # vol 2's cover → gallery
        ]
        with patch.object(metadata_router, "download_cover", AsyncMock(side_effect=covers)):
            with TestClient(self.app) as client:
                resp = client.post(
                    "/api/metadata/apply",
                    json={"target": "item", "target_id": self.multi_item_id,
                          "metadata": merged, "fields": fields},
                )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        item = body["item"]
        self.assertEqual(body["gallery_added"], 1)
        self.assertEqual(item["title"], "シリーズCD")
        # union: seeded hand-entered name survives, both volume CVs added
        self.assertIn("既存の人", item["seiyuu"])
        self.assertIn("A声優", item["seiyuu"])
        self.assertIn("B声優", item["seiyuu"])
        # per-volume note lines + count stamp
        self.assertIn("REC-851", item["notes"])
        self.assertIn("REC-852", item["notes"])
        self.assertIn("(2 volumes", item["notes"])
        self.assertEqual(item["cover_local"], "data/covers/MAN-TEST02.jpg")

        with TestClient(self.app) as client:
            # gallery listing with computed url
            resp = client.get(f"/api/items/{self.multi_item_id}/media")
            self.assertEqual(resp.status_code, 200)
            media = resp.json()["media"]
            self.assertEqual(len(media), 1)
            self.assertEqual(media[0]["url"], "/covers/MAN-TEST02_vol1.jpg")
            media_id = media[0]["id"]

        # re-apply: gallery dedupes, cast union doesn't duplicate
        with patch.object(metadata_router, "download_cover", AsyncMock(side_effect=covers)):
            with TestClient(self.app) as client:
                resp = client.post(
                    "/api/metadata/apply",
                    json={"target": "item", "target_id": self.multi_item_id,
                          "metadata": merged, "fields": ["seiyuu", "cover"]},
                )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["gallery_added"], 0)
        self.assertEqual(body["item"]["seiyuu"].count("A声優"), 1)

        with TestClient(self.app) as client:
            # promote the gallery image to primary cover
            resp = client.post(
                f"/api/items/{self.multi_item_id}/media/{media_id}/set-cover"
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["cover_local"], "data/covers/MAN-TEST02_vol1.jpg")
            # delete the gallery row
            resp = client.delete(f"/api/items/{self.multi_item_id}/media/{media_id}")
            self.assertEqual(resp.status_code, 200)
            resp = client.get(f"/api/items/{self.multi_item_id}/media")
            self.assertEqual(resp.json()["media"], [])

    def test_apply_adopt_code_promotes_manual_item(self):
        """Adopting a DLsite hit's code overrides the synthetic code and
        persists the carried scraper payload — no refetch."""

        async def _seed_item():
            async with aiosqlite.connect(database.DB_PATH) as conn:
                await conn.execute(
                    """INSERT INTO items (product_code, title, kind, is_manual,
                                          notes, created_at, updated_at)
                       VALUES ('MAN-ADOPT01', 'soine cd', 'drama_cd', 1, '', ?, ?)""",
                    (NOW, NOW),
                )
                await conn.commit()
                cur = await conn.execute(
                    "SELECT id FROM items WHERE product_code = 'MAN-ADOPT01'"
                )
                return (await cur.fetchone())[0]

        item_id = asyncio.run(_seed_item())
        meta = empty_metadata("dlsite", "https://www.dlsite.com/girls/work/=/product_id/RJ332208.html")
        meta["title"] = "週刊添い寝CD vol.1"
        meta["seiyuu"] = ["誰か"]
        meta["catalog_number"] = "RJ332208"
        meta["extra"]["product_code"] = "RJ332208"
        meta["extra"]["dlsite_metadata"] = {
            "title": "週刊添い寝CD vol.1",
            "title_en": "Weekly Soine CD vol.1",
            "circle": "メーカーX",
            "seiyuu": ["誰か"],
            "tags": ["癒し"],
            "release_date": "2021-06-01",
            "description": "おやすみ前のCD",
            "cover_local": "data/covers/RJ332208.jpg",
        }
        with TestClient(self.app) as client:
            resp = client.post(
                "/api/metadata/apply",
                json={"target": "item", "target_id": item_id,
                      "metadata": meta,
                      "fields": ["adopt_code", "source_note"]},
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertIn("adopt_code", body["applied"])
        item = body["item"]
        self.assertEqual(item["product_code"], "RJ332208")
        self.assertEqual(item["title"], "週刊添い寝CD vol.1")
        self.assertEqual(item["title_en"], "Weekly Soine CD vol.1")
        self.assertEqual(item["circle"], "メーカーX")
        self.assertIn("癒し", item["tags"])
        self.assertEqual(item["cover_local"], "data/covers/RJ332208.jpg")
        self.assertEqual(item["confidence"], "verified")
        self.assertIn("品番 RJ332208", item["notes"])

    def test_apply_adopt_code_conflict_409(self):
        """Adopting a code that already exists elsewhere is a 409."""
        meta = empty_metadata("dlsite", "https://www.dlsite.com/girls/work/=/product_id/RJ332208.html")
        meta["extra"]["product_code"] = "RJ332208"  # adopted by the test above... or seed our own

        async def _seed_two():
            async with aiosqlite.connect(database.DB_PATH) as conn:
                await conn.execute(
                    """INSERT OR IGNORE INTO items (product_code, title, kind, is_manual,
                                          notes, created_at, updated_at)
                       VALUES ('RJ999999', 'existing coded', 'drama_cd', 0, '', ?, ?)""",
                    (NOW, NOW),
                )
                await conn.execute(
                    """INSERT INTO items (product_code, title, kind, is_manual,
                                          notes, created_at, updated_at)
                       VALUES ('MAN-ADOPT02', 'conflict case', 'drama_cd', 1, '', ?, ?)""",
                    (NOW, NOW),
                )
                await conn.commit()
                cur = await conn.execute(
                    "SELECT id FROM items WHERE product_code = 'MAN-ADOPT02'"
                )
                return (await cur.fetchone())[0]

        item_id = asyncio.run(_seed_two())
        meta["extra"]["product_code"] = "RJ999999"
        with TestClient(self.app) as client:
            resp = client.post(
                "/api/metadata/apply",
                json={"target": "item", "target_id": item_id,
                      "metadata": meta, "fields": ["adopt_code"]},
            )
        self.assertEqual(resp.status_code, 409)

    def test_media_ownership_guard(self):
        """A media row belonging to another item can't be promoted/deleted
        through a different item id."""
        async def _seed_media():
            async with aiosqlite.connect(database.DB_PATH) as conn:
                cur = await conn.execute(
                    """INSERT INTO media_assets (parent_kind, parent_id, path, role,
                                                 sort_order, created_at)
                       VALUES ('item', ?, 'data/covers/owned.jpg', 'gallery', 0, ?)""",
                    (self.multi_item_id, NOW),
                )
                await conn.commit()
                return cur.lastrowid

        media_id = asyncio.run(_seed_media())
        with TestClient(self.app) as client:
            resp = client.post(f"/api/items/{self.item_id}/media/{media_id}/set-cover")
            self.assertEqual(resp.status_code, 404)
            resp = client.delete(f"/api/items/{self.item_id}/media/{media_id}")
            self.assertEqual(resp.status_code, 404)
            # cleanup via the rightful owner
            resp = client.delete(f"/api/items/{self.multi_item_id}/media/{media_id}")
            self.assertEqual(resp.status_code, 200)

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
