"""Parse-layer tests for metadata_sources, run against fixture HTML captured
from the live sites (2026-06). No network access."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import metadata_sources
from metadata_sources.animate import AnimateSource
from metadata_sources.base import empty_metadata, normalize_date
from metadata_sources.booth import BoothSource
from metadata_sources.chilchil import ChilChilSource
from metadata_sources.digiket import DigiketSource
from metadata_sources.dlsite import DLsiteSource
from metadata_sources.fanza import FanzaSource
from metadata_sources.gamers import GamersSource
from metadata_sources.gyutto import GyuttoSource
from metadata_sources.hvdb import HvdbSource
from metadata_sources.melon import MelonbooksSource
from metadata_sources.merge import merge_metadata
from metadata_sources.rejet import RejetSource
from metadata_sources.stellaworth import StellaworthSource

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
        self.assertIsInstance(
            metadata_sources.match_url("https://rejet.jp/works/?cat=41"),
            RejetSource,
        )
        self.assertIsInstance(
            metadata_sources.match_url(
                "https://www.dlsite.com/girls/work/=/product_id/RJ01465184.html"
            ),
            DLsiteSource,
        )
        self.assertIsInstance(
            metadata_sources.match_url("https://booth.pm/ja/items/1149620"),
            BoothSource,
        )
        self.assertIsInstance(
            metadata_sources.match_url("https://ykmrc.booth.pm/items/1149620"),
            BoothSource,
        )
        self.assertIsInstance(
            metadata_sources.match_url("https://www.animate-onlineshop.jp/pd/3465774/"),
            AnimateSource,
        )
        self.assertIsInstance(
            metadata_sources.match_url(
                "https://www.stellaworth.co.jp/shop/item.php?item_id=1nkCHY1d11d"
            ),
            StellaworthSource,
        )
        self.assertIsInstance(
            metadata_sources.match_url(
                "https://www.dmm.co.jp/dc/doujin/-/detail/=/cid=d_203316/"
            ),
            FanzaSource,
        )
        self.assertIsInstance(
            metadata_sources.match_url(
                "https://www.melonbooks.co.jp/detail/detail.php?product_id=200090"
            ),
            MelonbooksSource,
        )
        self.assertIsInstance(
            metadata_sources.match_url(
                "https://www.digiket.com/work/show/_data/ID=ITM0337679/"
            ),
            DigiketSource,
        )
        self.assertIsInstance(
            metadata_sources.match_url("https://gyutto.com/i/item282706"),
            GyuttoSource,
        )
        self.assertIsInstance(
            metadata_sources.match_url("https://hvdb.me/Dashboard/Details/01644019"),
            HvdbSource,
        )
        self.assertIsNone(metadata_sources.match_url("https://example.com/foo"))
        self.assertIsNone(metadata_sources.match_url(""))

    def test_list_sources_shape(self):
        sources = metadata_sources.list_sources()
        names = {s["name"] for s in sources}
        self.assertEqual(names, {
            "dlsite", "gamers", "chil_chil", "rejet",
            "booth", "animate", "stellaworth", "fanza", "melon",
            "digiket", "gyutto", "hvdb",
        })
        for s in sources:
            self.assertIsInstance(s["supports_search"], bool)
            self.assertTrue(s["url_example"])
        searchable = {s["name"] for s in sources if s["supports_search"]}
        # fanza is URL-paste only (search shape unverifiable behind the WAF)
        self.assertEqual(names - searchable, {"fanza"})


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


class BoothParseTests(unittest.TestCase):
    def setUp(self):
        self.source = BoothSource()

    def test_parse_product(self):
        meta = self.source.parse_product(
            fixture("booth_product.html"), "https://booth.pm/ja/items/1149620"
        )
        self.assertEqual(meta["source"], "booth")
        self.assertEqual(meta["title"], "ドラマCD")
        self.assertEqual(meta["maker"], "_")
        self.assertEqual(meta["price"], "1,500円")
        self.assertIn("booth.pximg.net", meta["cover_url"])
        self.assertIn("ゆきむら。", meta["description"])
        self.assertEqual(meta["extra"]["shop_url"], "https://ykmrc.booth.pm/")
        self.assertEqual(meta["extra"]["availability"], "OutOfStock")
        self.assertEqual(meta["extra"]["event"], "c95")

    def test_parse_search(self):
        hits = self.source.parse_search(fixture("booth_search.html"))
        self.assertGreater(len(hits), 10)
        for h in hits:
            self.assertIn("/items/", h["url"])
            self.assertTrue(h["title"])
        first = hits[0]
        self.assertTrue(first["thumbnail"])
        self.assertIn("¥", first["price"])
        self.assertTrue(first["category"])


class StellaworthParseTests(unittest.TestCase):
    def setUp(self):
        self.source = StellaworthSource()

    def test_parse_product(self):
        meta = self.source.parse_product(
            fixture("stellaworth_product.html"),
            "https://www.stellaworth.co.jp/shop/item.php?item_id=1nkCHY1d11d",
        )
        self.assertEqual(meta["source"], "stellaworth")
        self.assertIn("艶が〜るドラマCD 徳川慶喜", meta["title"])
        self.assertEqual(meta["jan"], "4589645320040")
        self.assertEqual(meta["maker"], "Moon Bear")
        self.assertIsNone(meta["release_date"])  # 未定
        self.assertIn("税込￥4,400", meta["price"])
        self.assertIsNone(meta["cover_url"])  # noimg placeholder skipped
        self.assertEqual(meta["extra"]["category"], "CD / ドラマCD")
        # tokuten box
        self.assertTrue(any("ブロマイド" in t for t in meta["extra"]["tokuten"]))
        self.assertTrue(any("ステラワース特典" in t for t in meta["extra"]["tokuten"]))
        # 【キャスト】 block: ・役名：声優名 lines
        self.assertIn("浪川大輔", meta["seiyuu"])
        self.assertIn("石田彰", meta["seiyuu"])
        self.assertIn("小岩井ことり", meta["seiyuu"])
        self.assertEqual(len(meta["seiyuu"]), 11)
        self.assertIn("艶が〜る", meta["description"])

    def test_parse_search(self):
        hits = self.source.parse_search(fixture("stellaworth_search.html"))
        self.assertGreaterEqual(len(hits), 5)
        first = hits[0]
        self.assertIn("艶が〜るドラマCD", first["title"])
        self.assertEqual(
            first["url"],
            "https://www.stellaworth.co.jp/shop/item.php?item_id=1nkCHY1d11d",
        )
        self.assertIsNone(first["thumbnail"])  # noimg placeholder
        self.assertIsNone(first["release_date"])  # 未定
        self.assertIn("税込￥4,400", first["price"])
        self.assertIn("CD", first["category"])
        self.assertIn("Moon Bear", first["category"])
        # an item with a real cover keeps its thumbnail
        self.assertTrue(any(h["thumbnail"] for h in hits))


class AnimateParseTests(unittest.TestCase):
    def setUp(self):
        self.source = AnimateSource()

    def test_parse_product(self):
        meta = self.source.parse_product(
            fixture("animate_product.html"),
            "https://www.animate-onlineshop.jp/pd/3465774/",
        )
        self.assertEqual(meta["source"], "animate")
        self.assertEqual(meta["title"], "【ドラマCD】イヤって言ってもきかないで")
        self.assertEqual(meta["price"], "5,940円")
        self.assertEqual(meta["release_date"], "2026-07-29")
        self.assertEqual(meta["catalog_number"], "CRWS-119")
        self.assertEqual(meta["jan"], "4560317788320")
        self.assertIn("techorus-cdn.com", meta["cover_url"])
        # ≪キャスト≫ block: "役名役 声優名" pairs
        self.assertEqual(meta["seiyuu"], ["阿座上洋平", "坂田将吾"])
        self.assertIn("あらすじ", meta["description"])
        self.assertNotIn("関連ワード", meta["description"])
        self.assertTrue(any("メーカー特典" in t for t in meta["extra"]["tokuten"]))
        self.assertIn("イヤって言ってもきかないで", meta["extra"]["keywords"])
        self.assertIn("予約受付中", meta["extra"]["availability"])

    def test_parse_search(self):
        hits = self.source.parse_search(fixture("animate_search.html"))
        self.assertGreater(len(hits), 10)
        first = hits[0]
        self.assertEqual(first["title"], "【ドラマCD】イヤって言ってもきかないで")
        self.assertTrue(first["url"].endswith("/pd/3465774/"))
        self.assertIn("techorus-cdn.com", first["thumbnail"])
        self.assertEqual(first["price"], "5,940円")
        self.assertEqual(first["release_date"], "2026-07-29")
        self.assertEqual(first["category"], "音楽")


class FanzaParseTests(unittest.TestCase):
    """Fixture is Wayback-archived markup (live site is region-blocked)."""

    def setUp(self):
        self.source = FanzaSource()

    def test_parse_product(self):
        meta = self.source.parse_product(
            fixture("fanza_product.html"),
            "https://www.dmm.co.jp/dc/doujin/-/detail/=/cid=d_203316/",
        )
        self.assertEqual(meta["source"], "fanza")
        self.assertIn("グルメレビューサイト", meta["title"])
        self.assertNotIn("30％OFF", meta["title"])  # campaign badge stripped
        self.assertEqual(meta["maker"], "ラブリテス")
        self.assertEqual(meta["release_date"], "2021-05-29")
        self.assertEqual(meta["price"], "880円")  # circle price, not campaign 616
        self.assertIn("doujin-assets.dmm.co.jp", meta["cover_url"])
        self.assertIn("d_203316", meta["cover_url"])
        self.assertIsNone(meta["series"])  # "----" skipped
        self.assertIn("genres", meta["extra"])
        self.assertIn("妊娠・孕ませ", meta["extra"]["genres"])
        self.assertIn("あらすじ", meta["description"])
        self.assertIn("rating", meta["extra"])
        self.assertTrue(meta["extra"]["rating"].startswith("4.13"))


class MelonbooksParseTests(unittest.TestCase):
    """Fixtures are Wayback-archived markup (live site WAF-blocks httpx)."""

    def setUp(self):
        self.source = MelonbooksSource()

    def test_parse_product(self):
        meta = self.source.parse_product(
            fixture("melon_product.html"),
            "https://www.melonbooks.co.jp/detail/detail.php?product_id=200090",
        )
        self.assertEqual(meta["source"], "melon")
        self.assertEqual(meta["title"], "輿水幸子の好き好きプロデューサー")
        self.assertEqual(meta["maker"], "あまみどころ")  # (作品数:24) stripped
        self.assertEqual(meta["extra"]["author"], "あまー")
        # shop 発売日 (2017/01/22) beats event 発行日 (2016/12/31)
        self.assertEqual(meta["release_date"], "2017-01-22")
        self.assertEqual(meta["price"], "628円")
        self.assertTrue(meta["cover_url"].startswith("https://melonbooks.akamaized.net/"))
        self.assertIn("誕生日の幸子", meta["description"])
        self.assertIn("コミックマーケット91", meta["extra"]["event"])
        self.assertIn("THE IDOLM@STER", meta["extra"]["genres"])
        self.assertIn("tags", meta["extra"])

    def test_parse_search(self):
        hits = self.source.parse_search(fixture("melon_search.html"))
        self.assertGreater(len(hits), 10)
        first = hits[0]
        self.assertEqual(first["title"], "恍惚に溺れる")
        self.assertIn("detail.php?product_id=2171429", first["url"])
        self.assertTrue(first["url"].startswith("https://www.melonbooks.co.jp/"))
        self.assertTrue(first["thumbnail"].startswith("https://melonbooks.akamaized.net/"))
        self.assertNotIn("now_printing", first["thumbnail"])
        self.assertEqual(first["price"], "1,980円")
        self.assertIn("Dishwasher1910", first["category"])


class DigiketParseTests(unittest.TestCase):
    def setUp(self):
        self.source = DigiketSource()

    def test_parse_product(self):
        meta = self.source.parse_product(
            fixture("digiket_product.html"),
            "https://www.digiket.com/work/show/_data/ID=ITM0337679/",
        )
        self.assertEqual(meta["source"], "digiket")
        self.assertEqual(meta["title"], "ブスなのにトライアングル！")
        self.assertEqual(meta["maker"], "きみりんこ。")
        self.assertEqual(meta["release_date"], "2026-05-02")  # 登録日
        self.assertEqual(meta["price"], "110円")
        self.assertEqual(meta["cover_url"], "https://img.digiket.net/cg/337/ITM0337679_1.jpg")
        self.assertIn("ヒロインはブス", meta["description"])
        self.assertEqual(meta["extra"]["work_type"], "一般向同人 ビジュアルノベル")
        self.assertIn("恋愛", meta["extra"]["tags"])

    def test_parse_search(self):
        hits = self.source.parse_search(fixture("digiket_search.html"))
        self.assertEqual(len(hits), 3)
        first = hits[0]
        self.assertIn("CV.安野希世乃", first["title"])
        self.assertEqual(first["url"], "https://www.digiket.com/work/show/_data/ID=ITM0263644/")
        self.assertTrue(first["thumbnail"].startswith("https://img.digiket.net/"))
        self.assertEqual(first["price"], "2530円")
        self.assertIn("あまかけプラント", first["category"])


class GyuttoParseTests(unittest.TestCase):
    def setUp(self):
        self.source = GyuttoSource()

    def test_parse_product(self):
        meta = self.source.parse_product(
            fixture("gyutto_product.html"), "https://gyutto.com/i/item282706"
        )
        self.assertEqual(meta["source"], "gyutto")
        self.assertIn("ＡＳＭＲレビュー", meta["title"])
        self.assertEqual(meta["maker"], "#define")
        self.assertEqual(meta["release_date"], "2026-03-27")
        self.assertEqual(meta["cover_url"],
                         "https://gyutto.com/data/item_img/2827/282706/282706.jpg")
        self.assertIsNone(meta["price"])  # price is AJAX-rendered on Gyutto
        self.assertIn("あにまちゃん", meta["description"])
        self.assertEqual(meta["extra"]["age_rating"], "18禁")
        self.assertEqual(meta["extra"]["work_type"], "音声作品")
        self.assertIn("オリジナル", meta["extra"]["genres"])
        self.assertIn("同人", meta["extra"]["category"])

    def test_parse_search(self):
        hits = self.source.parse_search(fixture("gyutto_search.html"))
        self.assertGreaterEqual(len(hits), 30)
        first = hits[0]
        self.assertEqual(first["url"], "http://gyutto.com/i/item270825")
        self.assertIn("耳かき", first["title"])
        self.assertTrue(first["thumbnail"].startswith("https://gyutto.com/data/item_img/"))
        self.assertEqual(first["price"], "2,090円")
        self.assertEqual(first["category"], "あうとどあ仙人")


class HvdbParseTests(unittest.TestCase):
    def setUp(self):
        self.source = HvdbSource()

    def test_parse_product_with_cvs(self):
        meta = self.source.parse_product(
            fixture("hvdb_product.html"), "https://hvdb.me/Dashboard/Details/01637794"
        )
        self.assertEqual(meta["source"], "hvdb")
        self.assertIn("黒髪クール姉", meta["title"])
        self.assertIsNone(meta["title_en"])  # not translated yet
        self.assertEqual(meta["extra"]["product_code"], "RJ01637794")
        self.assertEqual(meta["maker"], "M屋")
        self.assertEqual(meta["extra"]["circle_en"], "mya")
        self.assertEqual(meta["seiyuu"], ["田中"])
        self.assertEqual(meta["cover_url"], "https://hvdb.me/WorkImages/RJ01637794.jpg")
        self.assertIn("ear licking", meta["extra"]["tags"])
        self.assertFalse(meta["extra"]["sfw"])

    def test_parse_product_with_english_title(self):
        meta = self.source.parse_product(
            fixture("hvdb_product_en.html"), "https://hvdb.me/Dashboard/Details/206084"
        )
        self.assertEqual(meta["title"], "#011 あおい/19才(学生)")
        self.assertEqual(meta["title_en"], "#011 Aoi (19y/Student)")
        self.assertEqual(meta["extra"]["product_code"], "RJ206084")
        self.assertEqual(meta["maker"], "妖声堂")
        self.assertEqual(meta["seiyuu"], [])  # N/A filtered out

    def test_search_requires_rj_code(self):
        # search() short-circuits to [] for non-code queries without any
        # network access, so calling the coroutine directly is safe here.
        import asyncio

        async def run():
            return await self.source.search(None, "耳かき ASMR")

        self.assertEqual(asyncio.run(run()), [])


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


class RejetParseTests(unittest.TestCase):
    def setUp(self):
        self.source = RejetSource()

    def test_parse_listing_page(self):
        works = self.source.parse_page(
            fixture("rejet_list.html"), "https://rejet.jp/works/?cat=41"
        )
        self.assertGreaterEqual(len(works), 5)
        key, meta = works[0]
        self.assertEqual(key, "REC-861")
        self.assertEqual(meta["catalog_number"], "REC-861")
        self.assertIn("クリミナーレ", meta["title"])
        self.assertEqual(meta["release_date"], "2019-07-24")
        self.assertEqual(meta["price"], "2,200円+税")
        self.assertEqual(meta["series"], "クリミナーレ！")
        self.assertEqual(meta["maker"], "Rejet")
        # CV credit extracted from the title
        self.assertEqual(meta["seiyuu"], ["日野 聡"])
        # https-normalized cover with catalog filename
        self.assertTrue(meta["cover_url"].startswith("https://rejet.jp/"))
        self.assertIn("REC-861", meta["cover_url"])
        # addressable fragment URL
        self.assertEqual(meta["source_url"], "https://rejet.jp/works/?cat=41#rejet=REC-861")
        self.assertIn("48時間", meta["description"])
        self.assertEqual(meta["extra"]["type"], "CD")
        self.assertEqual(meta["extra"]["official_site"], "http://rejetweb.jp/criminalet/")

    def test_keys_unique_per_page(self):
        works = self.source.parse_page(
            fixture("rejet_list.html"), "https://rejet.jp/works/?cat=41"
        )
        keys = [k for k, _ in works]
        self.assertEqual(len(keys), len(set(keys)))

    def test_search_page_same_markup(self):
        works = self.source.parse_page(
            fixture("rejet_search.html"),
            "https://rejet.jp/works/?s=%E3%82%AF%E3%83%AA%E3%83%9F%E3%83%8A%E3%83%BC%E3%83%AC",
        )
        self.assertGreaterEqual(len(works), 5)
        for _, meta in works:
            self.assertTrue(meta["title"])


def _vol(n: int, cv: str, date: str, desc: str = "shared blurb") -> dict:
    meta = empty_metadata("rejet", f"https://rejet.jp/works/?s=x#rejet=REC-{850 + n}")
    meta["title"] = f"カレと48時間を駆け抜けるCD 「クリミナーレ！T」Vol.{n} CV.{cv}"
    meta["seiyuu"] = [cv]
    meta["release_date"] = date
    meta["description"] = desc
    meta["price"] = "2,200円+税"
    meta["catalog_number"] = f"REC-{850 + n}"
    meta["series"] = "クリミナーレ！"
    meta["maker"] = "Rejet"
    meta["cover_url"] = f"https://rejet.jp/works/wp-content/uploads/REC-{850 + n}_500.jpg"
    return meta


class DLsiteParseTests(unittest.TestCase):
    def setUp(self):
        self.source = DLsiteSource()

    def test_parse_search_girls(self):
        hits = self.source.parse_search(fixture("dlsite_search_girls.html"), "girls")
        self.assertGreaterEqual(len(hits), 20)
        first = hits[0]
        self.assertEqual(first["product_code"], "RJ01465185")
        self.assertIn("耳かき", first["title"])
        self.assertIn("/work/=/product_id/RJ01465185", first["url"])
        self.assertTrue(first["thumbnail"].startswith("https://img.dlsite.jp/"))
        self.assertEqual(first["price"], "880円")
        self.assertIn("DLsite girls", first["category"])
        self.assertIn("ボイス・ASMR", first["category"])
        # codes unique within a page
        codes = [h["product_code"] for h in hits]
        self.assertEqual(len(codes), len(set(codes)))

    def test_parse_search_maniax(self):
        hits = self.source.parse_search(fixture("dlsite_search_maniax.html"), "maniax")
        self.assertGreaterEqual(len(hits), 10)
        for h in hits:
            self.assertRegex(h["product_code"], r"^(RJ|BJ|VJ)\d+")


class MergeMetadataTests(unittest.TestCase):
    def test_merge_volumes(self):
        merged = merge_metadata([
            _vol(1, "鳥海浩輔", "2019-01-23"),
            _vol(2, "前野智昭", "2019-02-20"),
            _vol(3, "下野 紘", "2019-03-20"),
        ])
        # common prefix, stripped of the Vol.N tail
        self.assertEqual(merged["title"], "カレと48時間を駆け抜けるCD 「クリミナーレ！T」")
        # cast union in volume order
        self.assertEqual(merged["seiyuu"], ["鳥海浩輔", "前野智昭", "下野 紘"])
        # earliest release date
        self.assertEqual(merged["release_date"], "2019-01-23")
        # identical blurbs collapse to one
        self.assertEqual(merged["description"], "shared blurb")
        # identical price survives; differing catalog numbers don't
        self.assertEqual(merged["price"], "2,200円+税")
        self.assertIsNone(merged["catalog_number"])
        # first volume's cover is primary
        self.assertIn("REC-851", merged["cover_url"])
        # per-volume detail preserved
        vols = merged["extra"]["volumes"]
        self.assertEqual(len(vols), 3)
        self.assertEqual(vols[2]["catalog_number"], "REC-853")
        self.assertEqual(merged["series"], "クリミナーレ！")

    def test_merge_differing_descriptions_join_with_headers(self):
        merged = merge_metadata([
            _vol(1, "A", "2019-01-23", desc="story one"),
            _vol(2, "B", "2019-02-20", desc="story two"),
        ])
        self.assertIn("■", merged["description"])
        self.assertIn("story one", merged["description"])
        self.assertIn("story two", merged["description"])

    def test_merge_single_passthrough(self):
        v = _vol(1, "A", "2019-01-23")
        self.assertIs(merge_metadata([v]), v)

    def test_merge_unrelated_titles_falls_back_to_first(self):
        a = _vol(1, "A", "2019-01-23")
        b = _vol(2, "B", "2019-02-20")
        a["title"] = "全く別のタイトル"
        b["title"] = "another thing entirely"
        merged = merge_metadata([a, b])
        self.assertEqual(merged["title"], "全く別のタイトル")

    def test_merge_empty_raises(self):
        with self.assertRaises(ValueError):
            merge_metadata([])


if __name__ == "__main__":
    unittest.main()
