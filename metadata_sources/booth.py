"""BOOTH (booth.pm) metadata source.

Pixiv's doujin marketplace — primary home for circle-direct drama CD /
voice-drama sales. Product URLs (both forms redirect-stable):
    https://booth.pm/ja/items/{numeric}
    https://{shop}.booth.pm/items/{numeric}
Search: GET /ja/search/{keywords}. R-18 listings need the `adult=t`
consent cookie (static flag, no account).

Parsing leans on the JSON-LD Product block + og: tags — BOOTH's visual
classes are Tailwind-generated and churn, so only `js-`/BEM class hooks
are used in the DOM fallbacks."""
import json
import re
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

from .base import CV_RE as _CV_RE
from .base import MetadataSource, SourceError, empty_metadata

BASE = "https://booth.pm"


class BoothSource(MetadataSource):
    name = "booth"
    label = "BOOTH"
    url_example = "https://booth.pm/ja/items/1149620"
    supports_search = True
    _url_re = re.compile(r"(?:[\w-]+\.)?booth\.pm/(?:[a-z]{2}(?:-[a-z]{2})?/)?items/(\d+)", re.I)
    cookies = {"adult": "t"}  # R-18 consent gate

    # ------------------------------------------------------------- fetch
    async def fetch_by_url(self, client: httpx.AsyncClient, url: str) -> dict:
        m = self._url_re.search(url or "")
        if not m:
            raise SourceError(f"{self.label}: not an item URL: {url}")
        canonical = f"{BASE}/ja/items/{m.group(1)}"
        html = await self._get(client, canonical)
        return self.parse_product(html, canonical)

    async def search(self, client: httpx.AsyncClient, query: str) -> list[dict]:
        url = f"{BASE}/ja/search/{quote(query)}"
        html = await self._get(client, url)
        return self.parse_search(html)

    # ------------------------------------------------------------- parse
    def parse_product(self, html: str, source_url: str) -> dict:
        soup = BeautifulSoup(html, "html.parser")
        meta = empty_metadata(self.name, source_url)

        # JSON-LD Product block: name / description / image / offers.price /
        # brand{name,url}. The most stable thing on the page.
        ld = {}
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.string or "")
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(data, dict) and data.get("@type") == "Product":
                ld = data
                break

        meta["title"] = (ld.get("name") or "").strip() or None
        if not meta["title"]:
            og = soup.select_one('meta[property="og:title"][content]')
            if og:
                # "title - shop - BOOTH"
                meta["title"] = re.sub(r"\s*-\s*BOOTH\s*$", "", og["content"]).strip() or None

        brand = ld.get("brand") or {}
        if isinstance(brand, dict):
            meta["maker"] = (brand.get("name") or "").strip() or None
            if brand.get("url"):
                meta["extra"]["shop_url"] = brand["url"]

        image = ld.get("image")
        if isinstance(image, list):
            image = image[0] if image else None
        if not image:
            og = soup.select_one('meta[property="og:image"][content]')
            image = og["content"] if og else None
        meta["cover_url"] = image

        offers = ld.get("offers") or {}
        price = offers.get("price") if isinstance(offers, dict) else None
        if price:
            try:
                meta["price"] = f"{int(float(price)):,}円"
            except (TypeError, ValueError):
                meta["price"] = str(price)
        if isinstance(offers, dict) and offers.get("availability"):
            avail = offers["availability"].rsplit("/", 1)[-1]  # schema.org/InStock
            meta["extra"]["availability"] = avail

        # Full description body lives in a js-hook div; JSON-LD copy can be
        # truncated on long listings.
        desc_el = soup.select_one(".js-market-item-detail-description")
        desc = desc_el.get_text("\n", strip=True) if desc_el else None
        meta["description"] = desc or (ld.get("description") or "").strip() or None

        if meta["description"]:
            for name in _CV_RE.findall(meta["description"]):
                for part in re.split(r"[、,，・]", name):
                    part = part.strip()
                    if part and part not in meta["seiyuu"]:
                        meta["seiyuu"].append(part)

        # Tracking attrs carry the shop slug + event (e.g. "c95").
        market = soup.select_one("div.market[data-product-id]")
        if market:
            if market.get("data-product-event"):
                meta["extra"]["event"] = market["data-product-event"]
            if market.get("data-product-brand") and not meta["extra"].get("shop_url"):
                meta["extra"]["shop_url"] = f"https://{market['data-product-brand']}.booth.pm/"

        breadcrumbs = [
            a.get_text(strip=True)
            for a in soup.select("#js-item-category-breadcrumbs a")
            if a.get_text(strip=True)
        ]
        if breadcrumbs:
            meta["extra"]["category"] = " > ".join(breadcrumbs)

        return meta

    def parse_search(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        hits = []
        for li in soup.select("li.item-card"):
            a = li.select_one("a[href*='/items/']")
            if not a:
                continue
            title = li.select_one(".item-card__title")
            hit = {
                "source": self.name,
                "title": (title.get_text(strip=True) if title
                          else li.get("data-product-name") or ""),
                "url": a["href"],
                "thumbnail": None,
                "release_date": None,
                "price": None,
                "category": None,
            }
            if not hit["title"]:
                continue
            thumb = li.select_one("a.js-thumbnail-image")
            if thumb:
                hit["thumbnail"] = thumb.get("data-original")
                if not hit["thumbnail"]:
                    style = thumb.get("style") or ""
                    m = re.search(r"url\((['\"]?)(.+?)\1\)", style)
                    if m:
                        hit["thumbnail"] = m.group(2)
            price = li.select_one("div.price")
            if price:
                hit["price"] = price.get_text(strip=True).replace("¥ ", "¥")
            cat = li.select_one(".item-card__category a, a.item-card__category-anchor")
            shop = li.select_one(".item-card__shop-name")
            parts = [el.get_text(strip=True) for el in (cat, shop) if el and el.get_text(strip=True)]
            if parts:
                hit["category"] = " / ".join(parts)
            hits.append(hit)
        return hits
