"""Gamers (gamers.co.jp) metadata source.

Server-rendered EC site with a dedicated drama CD category. Product URLs:
    https://www.gamers.co.jp/pd/{numeric}/
    https://www.gamers.co.jp/pn/{urlencoded-title}/pd/{numeric}/
Search: GET /products/list.php?mode=search&smt={keywords}"""
import re
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup

from .base import CV_RE as _CV_RE
from .base import MetadataSource, SourceError, empty_metadata, normalize_date

BASE = "https://www.gamers.co.jp"


class GamersSource(MetadataSource):
    name = "gamers"
    label = "Gamers"
    url_example = "https://www.gamers.co.jp/pd/10890803/"
    supports_search = True
    _url_re = re.compile(r"gamers\.co\.jp/(?:pn/[^/]+/)?pd/(\d+)/?", re.I)

    # ------------------------------------------------------------- fetch
    async def fetch_by_url(self, client: httpx.AsyncClient, url: str) -> dict:
        m = self._url_re.search(url or "")
        if not m:
            raise SourceError(f"{self.label}: not a product URL: {url}")
        canonical = f"{BASE}/pd/{m.group(1)}/"
        html = await self._get(client, canonical)
        return self.parse_product(html, canonical)

    async def search(self, client: httpx.AsyncClient, query: str) -> list[dict]:
        url = f"{BASE}/products/list.php?mode=search&smt={quote(query)}"
        html = await self._get(client, url)
        return self.parse_search(html)

    # ------------------------------------------------------------- parse
    def parse_product(self, html: str, source_url: str) -> dict:
        soup = BeautifulSoup(html, "html.parser")
        meta = empty_metadata(self.name, source_url)

        h1 = soup.select_one("#item_detail h1.ttl_style01") or soup.select_one("h1.ttl_style01")
        if h1:
            meta["title"] = h1.get_text(strip=True)

        # Cover: prefer the full-size product image over the og:image (which
        # carries forced 1200x630 resize params).
        img = soup.select_one("ul.item_img_main img[src]")
        if img:
            meta["cover_url"] = img["src"]
        else:
            og = soup.select_one('meta[property="og:image"][content]')
            if og:
                meta["cover_url"] = og["content"]

        price = soup.select_one(".item_detail_price p.price span")
        if price:
            text = price.get_text(strip=True)
            meta["price"] = text if text.endswith("円") else text + "円"

        release = soup.select_one(".item_detail_release p.release")
        if release:
            meta["release_date"] = normalize_date(release.get_text(" ", strip=True))

        # 品番 (catalog number) sits in free text: "品番: SNCL-119"
        m = re.search(r"品番\s*[:：]\s*([A-Za-z0-9][A-Za-z0-9\-]+)", html)
        if m:
            meta["catalog_number"] = m.group(1)

        content = soup.select_one(".item_detail_content_inner")
        if content:
            desc = content.get_text("\n", strip=True)
            # Strip the boilerplate cart-button disclaimer Gamers injects.
            desc = re.sub(r"※下記商品が「発売日以降出荷」.*?ございます。", "", desc, flags=re.S)
            meta["description"] = desc.strip() or None
            for name in _CV_RE.findall(desc):
                for part in re.split(r"[、,，・]", name):
                    part = part.strip()
                    if part and part not in meta["seiyuu"]:
                        meta["seiyuu"].append(part)

        # Store-exclusive bonus blurbs (特典情報)
        tokuten = [
            t.get_text(strip=True)
            for t in soup.select(".item_detail_class .tokuten_name")
            if t.get_text(strip=True)
        ]
        if tokuten:
            # de-dup: the section renders twice (pc + sp variants)
            meta["extra"]["tokuten"] = list(dict.fromkeys(tokuten))

        keywords = [
            a.get_text(strip=True)
            for a in soup.select('.item_detail_other .items_label a[href*="/keyword/"]')
            if a.get_text(strip=True)
        ]
        if keywords:
            meta["extra"]["keywords"] = list(dict.fromkeys(keywords))

        sell = soup.select_one(".item_detail_release p.sell span")
        if sell:
            meta["extra"]["availability"] = sell.get_text(strip=True)

        return meta

    def parse_search(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        hits = []
        for li in soup.select("ul.search_item_list li.list_product"):
            a = li.select_one('a[href*="/pd/"]')
            title = li.select_one("h3.item_list_ttl")
            if not a or not title:
                continue
            hit = {
                "source": self.name,
                "title": title.get_text(strip=True),
                "url": urljoin(BASE, a["href"]),
                "thumbnail": None,
                "release_date": None,
                "price": None,
                "category": None,
            }
            img = li.select_one(".item_list_thumb img")
            if img:
                hit["thumbnail"] = img.get("data-original") or img.get("src")
            price = li.select_one(".item_list_detail p.price")
            if price:
                hit["price"] = price.get_text(strip=True)
            release = li.select_one(".item_list_detail p.release")
            if release:
                hit["release_date"] = normalize_date(release.get_text(strip=True))
            hits.append(hit)
        return hits
