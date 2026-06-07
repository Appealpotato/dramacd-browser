"""DLsite title-search metadata source.

The DLsite flow for CODED items lives in scraper.py and is untouched. This
source exists for the codeless world: search DLsite by title (girls +
maniax sections — fsr result pages are server-rendered), and fetch a single
work by product URL. fetch_by_url delegates to scraper.fetch_metadata_for_code
so the result is exactly what a normal scan-fetch would produce; the full
scraper payload rides along in extra['dlsite_metadata'] so the apply layer's
adopt-code path can persist it without refetching.

Hits carry extra-less flat fields plus the RJ/BJ/VJ code — the preview's
"adopt code" checkbox uses it to promote a manual item into a fully coded
one (override_product_code + update_item_metadata)."""
import asyncio
import re
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

from .base import HEADERS, MetadataSource, SourceError, empty_metadata, normalize_date

BASE = "https://www.dlsite.com"
# Sections worth searching for drama CDs. girls first (otome/BL libraries),
# then maniax (male-audience doujin). fsr pages are server-rendered in both.
SEARCH_SECTIONS = ("girls", "maniax")

_CODE_RE = re.compile(r"product_id/((?:RJ|BJ|VJ)\d{6,8})", re.I)
_THUMB_RE = re.compile(r"(//img\.dlsite\.jp/[^'\"\]]+?_img_main_240x240\.jpg)")


class DLsiteSource(MetadataSource):
    name = "dlsite"
    label = "DLsite"
    url_example = "https://www.dlsite.com/girls/work/=/product_id/RJ01465184.html"
    supports_search = True
    _url_re = re.compile(
        r"dlsite\.com/[a-z0-9\-]+/work/=/product_id/((?:RJ|BJ|VJ)\d{6,8})", re.I
    )

    # ------------------------------------------------------------- fetch
    async def fetch_by_url(self, client: httpx.AsyncClient, url: str) -> dict:
        m = self._url_re.search(url or "")
        if not m:
            raise SourceError(f"{self.label}: not a work URL: {url}")
        code = m.group(1).upper()
        # Reuse the scan pipeline's scraper end-to-end (JSON API + page
        # enrichment + EN locale + cover download keyed by the code).
        from scraper import fetch_metadata_for_code

        scraped, reason = await fetch_metadata_for_code(client, code)
        if not scraped:
            raise SourceError(f"{self.label}: fetch failed for {code} ({reason})")

        meta = empty_metadata(self.name, url)
        meta["title"] = scraped.get("title")
        meta["title_en"] = scraped.get("title_en")
        meta["seiyuu"] = list(scraped.get("seiyuu") or [])
        meta["description"] = scraped.get("description")
        meta["release_date"] = normalize_date(scraped.get("release_date"))
        meta["cover_url"] = scraped.get("cover_url")
        meta["maker"] = scraped.get("circle")
        meta["series"] = scraped.get("series")
        meta["catalog_number"] = scraped.get("actual_code") or code
        meta["extra"]["product_code"] = scraped.get("actual_code") or code
        meta["extra"]["tags"] = scraped.get("tags") or []
        meta["extra"]["age_rating"] = scraped.get("age_rating")
        # Full scraper payload for the adopt-code path. `raw` is dropped to
        # keep the preview JSON light; refresh-metadata can repopulate it.
        dl_meta = {k: v for k, v in scraped.items() if k != "raw"}
        meta["extra"]["dlsite_metadata"] = dl_meta
        return meta

    async def search(self, client: httpx.AsyncClient, query: str) -> list[dict]:
        hits: list[dict] = []
        seen_codes: set[str] = set()
        # Multi-word keywords MUST join with '+' — a %20 space in the keyword
        # path segment trips DLsite's WAF into a 403.
        keyword = "+".join(quote(part) for part in (query or "").split())
        for i, section in enumerate(SEARCH_SECTIONS):
            if i:
                await asyncio.sleep(0.4)  # stay polite across sections
            url = (
                f"{BASE}/{section}/fsr/=/language/jp/keyword/{keyword}"
                f"/order%5B0%5D/trend/per_page/30/"
            )
            try:
                resp = await client.get(
                    url,
                    headers={**HEADERS, "Cookie": "adultchecked=1"},
                    follow_redirects=True, timeout=30,
                )
                if resp.status_code != 200:
                    continue
            except httpx.HTTPError:
                continue
            for hit in self.parse_search(resp.text, section):
                code = hit.get("product_code")
                if code and code in seen_codes:
                    continue
                if code:
                    seen_codes.add(code)
                hits.append(hit)
        return hits

    # ------------------------------------------------------------- parse
    def parse_search(self, html: str, section: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        hits = []
        for li in soup.select("li[data-list_item_product_id]"):
            name_a = li.select_one(".work_name a[href]")
            if not name_a:
                continue
            code = (li.get("data-list_item_product_id") or "").upper()
            if not code:
                m = _CODE_RE.search(name_a["href"])
                code = m.group(1).upper() if m else None
            hit = {
                "source": self.name,
                "title": name_a.get("title") or name_a.get_text(strip=True),
                "url": name_a["href"],
                "thumbnail": None,
                "release_date": None,
                "price": None,
                "category": None,
                "product_code": code,
            }
            # Thumbnail hides in the Vue component's :thumb-candidates attr.
            tm = _THUMB_RE.search(str(li))
            if tm:
                hit["thumbnail"] = "https:" + tm.group(1)
            price = li.select_one(".work_price_base")
            if price and price.get_text(strip=True):
                hit["price"] = price.get_text(strip=True) + "円"
            category = li.select_one(".work_category a")
            maker = li.select_one(".maker_name a")
            parts = [p for p in (
                f"DLsite {section}",
                category.get_text(strip=True) if category else None,
                maker.get_text(strip=True) if maker else None,
            ) if p]
            hit["category"] = " / ".join(parts)
            hits.append(hit)
        return hits
