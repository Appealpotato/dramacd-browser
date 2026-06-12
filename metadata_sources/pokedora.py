"""Pokedora / Pocket Drama CD (pokedora.com) metadata source.

Animate's drama-CD / situation-CD download store. Product URLs:
    https://pokedora.com/products/detail.php?product_id={numeric}

Adult products sit behind an age gate: fetched WITHOUT ``age_check=1`` the page
returns a generic stub (its <title> is just "商品詳細ページ" and it's ~half the
size), so we always append ``age_check=1`` to the fetch URL. Price and release
date are rendered client-side and aren't in the static HTML, so they stay None.
"""
import re
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .base import MetadataSource, SourceError, empty_metadata

BASE = "https://pokedora.com"


class PokedoraSource(MetadataSource):
    name = "pokedora"
    label = "Pokedora"
    url_example = "https://pokedora.com/products/detail.php?product_id=85510"
    supports_search = False
    # `product_id` must start a query param ((?:^|[?&]) after the optional
    # prefix) — otherwise the lazy prefix lets it match as a SUBSTRING of
    # `watch_product_id=`/`related_product_id=` and capture the wrong product.
    _url_re = re.compile(
        r"pokedora\.com/products/detail\.php\?(?:[^#\s]*[?&])?product_id=(\d+)", re.I
    )

    # ------------------------------------------------------------- fetch
    async def fetch_by_url(self, client: httpx.AsyncClient, url: str) -> dict:
        m = self._url_re.search(url or "")
        if not m:
            raise SourceError(f"{self.label}: not a product URL: {url}")
        pid = m.group(1)
        # age_check=1 clears the adult gate; without it the page is a stub.
        fetch_url = f"{BASE}/products/detail.php?product_id={pid}&age_check=1"
        html = await self._get(client, fetch_url)
        canonical = f"{BASE}/products/detail.php?product_id={pid}"
        return self.parse_product(html, canonical)

    # ------------------------------------------------------------- parse
    def parse_product(self, html: str, source_url: str) -> dict:
        soup = BeautifulSoup(html, "html.parser")
        meta = empty_metadata(self.name, source_url)

        # Title lives in og:title / <title> as "<product> | <maker> | ポケドラ".
        # Take the first segment and drop the trailing 【出演声優：…】 cast bracket.
        raw_title = None
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            raw_title = og_title["content"]
        elif soup.title:
            raw_title = soup.title.get_text(strip=True)
        if raw_title:
            head = re.split(r"[|｜]", raw_title)[0].strip()
            head = re.sub(r"【[^】]*】\s*$", "", head).strip()
            meta["title"] = head or None

        # Cover art (get_image.php). og:image is an absolute URL already.
        og_image = soup.find("meta", property="og:image")
        if og_image and og_image.get("content"):
            meta["cover_url"] = urljoin(BASE, og_image["content"].strip())

        # No dedicated description block on the page; og:description is the
        # store's stock blurb with the cast/genre embedded — keep it as a
        # fallback so the field isn't empty.
        og_desc = soup.find("meta", property="og:description")
        if og_desc and og_desc.get("content"):
            meta["description"] = og_desc["content"].strip() or None

        # Spec rows render as:
        #   <div class="item_detail_extra_header_wrap">
        #     <span class="item_detail_extra_header">出演声優</span>：
        #   </div>
        #   <... value with <a> links ...>
        # i.e. the value is the wrap's next non-empty sibling, not a child.
        for wrap in soup.select(".item_detail_extra_header_wrap"):
            hdr = wrap.select_one(".item_detail_extra_header")
            if not hdr:
                continue
            label = hdr.get_text(strip=True)
            sib = wrap.find_next_sibling()
            while sib is not None and not sib.get_text(strip=True):
                sib = sib.find_next_sibling()
            if sib is None:
                continue
            links = [a.get_text(strip=True) for a in sib.select("a") if a.get_text(strip=True)]
            text = sib.get_text(" ", strip=True)
            values = links or ([text] if text else [])
            if not values:
                continue
            if "声優" in label or "出演" in label:
                meta["seiyuu"] = values
            elif "シリーズ" in label:
                meta["series"] = values[0]
            elif "レーベル" in label or "メーカー" in label:
                meta["maker"] = values[0]
            elif "関連" in label or "ワード" in label:
                meta["extra"]["tags"] = values

        return meta
