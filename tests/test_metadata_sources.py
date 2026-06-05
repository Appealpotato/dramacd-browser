"""Parse-layer tests for metadata_sources, run against fixture HTML captured
from the live sites (2026-06). No network access."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import metadata_sources
from metadata_sources.base import normalize_date
from metadata_sources.chilchil import ChilChilSource
from metadata_sources.gamers import GamersSource

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class RegistryTests(unittest.TestCase):
    def test_match_url_dispatches_to_right_source(self):
        self.assertIsInstance(
            metadata_sources.match_url("https://www.gamers.co.jp/pd/10890803/"),
            GamersSource,
        )
        self.assertIsInstance(
            metadata_sources.match_url(
                "https://www.gamers.co.jp/pn/%E3%83%89%E3%83%A9%E3%83%9ECD/pd/10890803/"
            ),
            GamersSource,
        )
        self.assertIsInstance(
            metadata_sources.match_url("https://www.chil-chil.net/goodsDetail/goods_id/23936/"),
            ChilChilSource,
        )
        self.assertIsNone(metadata_sources.match_url("https://example.com/foo"))
        self.assertIsNone(metadata_sources.match_url(""))

    def test_list_sources_shape(self):
        sources = metadata_sources.list_sources()
        names = {s["name"] for s in sources}
        self.assertEqual(names, {"gamers", "chil_chil"})
        for s in sources:
            self.assertTrue(s["supports_search"])
            self.assertTrue(s["url_example"])


class NormalizeDateTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(normalize_date("2026/09/09 発売"), "2026-09-09")
        self.assertEqual(normalize_date("2009-03-27"), "2009-03-27")
        self.assertEqual(normalize_date("2008年9月1日"), "2008-09-01")
        self.assertIsNone(normalize_date("発売日未定"))
        self.assertIsNone(normalize_date(None))


class GamersParseTests(unittest.TestCase):
    def setUp(self):
        self.source = GamersSource()

    def test_parse_product(self):
        meta = self.source.parse_product(
            fixture("gamers_product.html"), "https://www.gamers.co.jp/pd/10896456/"
        )
        self.assertEqual(meta["source"], "gamers")
        self.assertIn("超かぐや姫", meta["title"])
        self.assertEqual(meta["release_date"], "2026-09-09")
        self.assertEqual(meta["price"], "21,200円")
        self.assertEqual(meta["catalog_number"], "SNCL-119")
        self.assertIn("techorus-cdn.com", meta["cover_url"])
        self.assertIn("tokuten", meta["extra"])
        self.assertIn("B2布ポスター", meta["extra"]["tokuten"])
        self.assertIn("keywords", meta["extra"])
        self.assertIn("超かぐや姫!", meta["extra"]["keywords"])

    def test_parse_search(self):
        hits = self.source.parse_search(fixture("gamers_search.html"))
        self.assertGreater(len(hits), 10)
        first = hits[0]
        self.assertIn("ドラマCD", first["title"])
        self.assertIn("/pd/", first["url"])
        self.assertTrue(first["url"].startswith("https://www.gamers.co.jp/"))
        self.assertTrue(first["thumbnail"])
        self.assertRegex(first["release_date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertIn("円", first["price"])


class ChilChilParseTests(unittest.TestCase):
    def setUp(self):
        self.source = ChilChilSource()

    def test_parse_cd_product(self):
        meta = self.source.parse_product(
            fixture("chilchil_cd.html"),
            "https://www.chil-chil.net/goodsDetail/goods_id/23936/",
            23936,
        )
        self.assertEqual(meta["title"], "どうしても触れたくない")
        self.assertEqual(meta["release_date"], "2009-03-27")
        self.assertEqual(meta["jan"], "4961524410477")
        self.assertIn("ムービック", meta["maker"])
        self.assertEqual(meta["series"], "どうしても触れたくない")
        # main couple cast present, ordered, de-duped
        self.assertIn("石川英郎", meta["seiyuu"])
        self.assertIn("野島健児", meta["seiyuu"])
        self.assertIn("森川智之", meta["seiyuu"])
        self.assertEqual(len(meta["seiyuu"]), len(set(meta["seiyuu"])))
        # structured cast detail per story
        detail = meta["extra"]["cast_detail"]
        self.assertGreaterEqual(len(detail), 2)
        roles = {c["role"] for c in detail[0]["cast"]}
        self.assertIn("攻", roles)
        self.assertIn("受", roles)
        other = [c for c in detail[0]["cast"] if c["role"] == "other"]
        self.assertTrue(any(c["actor"] == "桑原敬一" for c in other))
        # synopsis + work info extras
        self.assertIn("外川", meta["description"])
        self.assertEqual(meta["extra"]["work_info"].get("収録時間"), "132 分")
        # The detail page exposes the XL rendition — parser prefers what the
        # page offers over the constructed L fallback.
        self.assertEqual(meta["cover_url"], "https://img.chil-chil.net/goods_img/XL/00023936_XL.jpg")

    def test_parse_manga_product_no_cast(self):
        """Non-CD goods pages still parse the shared fields."""
        meta = self.source.parse_product(
            fixture("chilchil_manga.html"),
            "https://www.chil-chil.net/goodsDetail/goods_id/18653/",
            18653,
        )
        self.assertEqual(meta["title"], "どうしても触れたくない")
        self.assertEqual(meta["release_date"], "2008-09-01")
        self.assertEqual(meta["seiyuu"], [])
        self.assertIn("嶋", meta["description"])
        self.assertIn("大洋図書", meta["maker"])

    def test_parse_search(self):
        hits = self.source.parse_search(fixture("chilchil_search.html"))
        self.assertGreaterEqual(len(hits), 5)
        titles = [h["title"] for h in hits]
        self.assertIn("どうしても触れたくない", titles)
        cd_hits = [h for h in hits if h["category"] == "CD"]
        self.assertTrue(cd_hits, f"expected a CD-category hit, got {[h['category'] for h in hits]}")
        for h in hits:
            self.assertTrue(h["url"].startswith("https://www.chil-chil.net/goodsDetail/"))


if __name__ == "__main__":
    unittest.main()
