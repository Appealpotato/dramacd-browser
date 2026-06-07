"""Tests for the full DLsite work-page HTML parser and the Wayback
fallback hook in scraper.fetch_metadata_for_code.

The parser fixture is a live work page captured 2026-06; the hook tests
mock the network entirely."""
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio

import scraper
from metadata_sources.dlsite_html import (
    code_from_work_url,
    looks_like_work_page,
    parse_dlsite_work_html,
)

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class ParseDlsiteWorkHtmlTests(unittest.TestCase):
    def setUp(self):
        self.html = fixture("dlsite_work_page.html")

    def test_full_parse(self):
        meta = parse_dlsite_work_html(
            self.html,
            source_url="https://www.dlsite.com/girls/work/=/product_id/RJ01465184.html",
        )
        self.assertIn("明らか危ないお兄さん", meta["title"])
        self.assertEqual(meta["circle"], "がるまにオリジナル(乙女)")
        self.assertEqual(
            meta["cover_url"],
            "https://img.dlsite.jp/modpub/images2/work/doujin/RJ01466000/RJ01465184_img_main.jpg",
        )
        self.assertEqual(meta["release_date"], "2025-09-24")
        self.assertEqual(meta["series"], "がるまにカレシ")
        self.assertEqual(meta["seiyuu"], ["主水Ash"])
        self.assertEqual(meta["age_rating"], "全年齢")
        self.assertIn("ASMR", meta["tags"])
        self.assertIn("癒し", meta["tags"])
        self.assertIn("がるまにカレシ", meta["description"])
        self.assertEqual(
            meta["source_page"],
            "https://www.dlsite.com/girls/work/=/product_id/RJ01465184.html",
        )

    def test_partial_html_is_safe(self):
        """Archived pages can be truncated — every selector is guarded."""
        meta = parse_dlsite_work_html("<html><body><p>nothing here</p></body></html>")
        self.assertEqual(meta, {})

    def test_title_only(self):
        meta = parse_dlsite_work_html('<h1 id="work_name">作品名</h1>')
        self.assertEqual(meta, {"title": "作品名"})

    def test_looks_like_work_page(self):
        self.assertTrue(looks_like_work_page(self.html))
        self.assertFalse(looks_like_work_page("<html><body>404</body></html>"))
        self.assertFalse(looks_like_work_page(""))

    def test_code_from_work_url(self):
        self.assertEqual(
            code_from_work_url("https://www.dlsite.com/maniax/work/=/product_id/RJ123456.html"),
            "RJ123456",
        )
        self.assertEqual(
            code_from_work_url("https://web.archive.org/web/2021id_/https://www.dlsite.com/girls/work/=/product_id/rj01465184.html"),
            "RJ01465184",
        )
        self.assertIsNone(code_from_work_url("https://example.com/x"))


class WaybackHookTests(unittest.TestCase):
    """fetch_metadata_for_code falls back to Wayback only on hard 404s."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_fires_on_not_found(self):
        wb_meta = {
            "title": "救出された作品",
            "tags": ["ASMR"],
            "seiyuu": ["誰か"],
            "cover_url": "https://web.archive.org/web/2021im_/https://img.dlsite.jp/x.jpg",
            "raw": {"_wayback": {"snapshot_url": "s", "timestamp": "2021",
                                 "original_url": "o"}},
        }
        with patch.object(scraper, "fetch_product_json",
                          AsyncMock(return_value=(None, "not_found"))), \
             patch.object(scraper, "fetch_dlsite_via_wayback",
                          AsyncMock(return_value=dict(wb_meta))) as wb, \
             patch.object(scraper, "download_cover",
                          AsyncMock(return_value=("data/covers/RJ999999.jpg", None))), \
             patch("scraper.asyncio.sleep", AsyncMock()):
            meta, reason = self._run(
                scraper.fetch_metadata_for_code(None, "RJ999999", wayback=True)
            )
        wb.assert_awaited_once()
        self.assertIsNone(reason)
        self.assertEqual(meta["title"], "救出された作品")
        # EN fields mirrored from JP (no EN pass for archived pages)
        self.assertEqual(meta["title_en"], "救出された作品")
        self.assertEqual(meta["tags_en"], ["ASMR"])
        self.assertEqual(meta["seiyuu_en"], ["誰か"])
        # cover downloaded through the archive
        self.assertEqual(meta["cover_local"], "data/covers/RJ999999.jpg")
        # provenance preserved for metadata_raw
        self.assertIn("_wayback", meta["raw"])

    def test_does_not_fire_on_rate_limited(self):
        with patch.object(scraper, "fetch_product_json",
                          AsyncMock(return_value=(None, "rate_limited"))), \
             patch.object(scraper, "fetch_dlsite_via_wayback", AsyncMock()) as wb, \
             patch("scraper.asyncio.sleep", AsyncMock()):
            meta, reason = self._run(
                scraper.fetch_metadata_for_code(None, "RJ999999", wayback=True)
            )
        wb.assert_not_awaited()
        self.assertIsNone(meta)
        self.assertEqual(reason, "rate_limited")

    def test_respects_wayback_flag_off(self):
        with patch.object(scraper, "fetch_product_json",
                          AsyncMock(return_value=(None, "not_found"))), \
             patch.object(scraper, "fetch_dlsite_via_wayback", AsyncMock()) as wb, \
             patch("scraper.asyncio.sleep", AsyncMock()):
            meta, reason = self._run(
                scraper.fetch_metadata_for_code(None, "RJ999999", wayback=False)
            )
        wb.assert_not_awaited()
        self.assertIsNone(meta)
        self.assertEqual(reason, "not_found")

    def test_wayback_miss_keeps_not_found(self):
        with patch.object(scraper, "fetch_product_json",
                          AsyncMock(return_value=(None, "not_found"))), \
             patch.object(scraper, "fetch_dlsite_via_wayback",
                          AsyncMock(return_value=None)) as wb, \
             patch("scraper.asyncio.sleep", AsyncMock()):
            meta, reason = self._run(
                scraper.fetch_metadata_for_code(None, "RJ999999", wayback=True)
            )
        wb.assert_awaited_once()
        self.assertIsNone(meta)
        self.assertEqual(reason, "not_found")


if __name__ == "__main__":
    unittest.main()
