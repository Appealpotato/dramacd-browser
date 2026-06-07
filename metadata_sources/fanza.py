"""FANZA Doujin (dmm.co.jp/dc/doujin) metadata source.

DLsite's main competitor for doujin voice/drama works — useful when a work
was delisted from DLsite but still sells on FANZA. Product URLs:
    https://www.dmm.co.jp/dc/doujin/-/detail/=/cid={cid}/
Needs the `age_check_done=1` consent cookie (static flag, no account).

AT-RISK: DMM region-blocks the adult domain (non-JP IPs get bounced to a
login page) — verified blocked from this network 2026-06. The parser is
fixture-tested against Wayback-archived markup (2022 era); live fetches
work only from a JP network. No search support: the search URL shape
could not be verified against the live site."""
import json
import re

import httpx
from bs4 import BeautifulSoup

from .base import CV_RE as _CV_RE
from .base import MetadataSource, SourceError, empty_metadata, normalize_date

BASE = "https://www.dmm.co.jp"


class FanzaSource(MetadataSource):
    name = "fanza"
    label = "FANZA Doujin"
    url_example = "https://www.dmm.co.jp/dc/doujin/-/detail/=/cid=d_203316/"
    supports_search = False
    _url_re = re.compile(r"dmm\.co\.jp/dc/doujin/-/detail/=/cid=([a-z0-9_]+)", re.I)
    cookies = {"age_check_done": "1"}  # adult consent gate

    # ------------------------------------------------------------- fetch
    async def fetch_by_url(self, client: httpx.AsyncClient, url: str) -> dict:
        m = self._url_re.search(url or "")
        if not m:
            raise SourceError(f"{self.label}: not a product URL: {url}")
        canonical = f"{BASE}/dc/doujin/-/detail/=/cid={m.group(1)}/"
        html = await self._get(client, canonical)
        return self.parse_product(html, canonical)

    # ------------------------------------------------------------- parse
    def parse_product(self, html: str, source_url: str) -> dict:
        soup = BeautifulSoup(html, "html.parser")
        meta = empty_metadata(self.name, source_url)

        h1 = soup.select_one("h1.productTitle__txt")
        if h1:
            # Campaign badge (【30％OFF】) is a nested span — drop it.
            for span in h1.select("span"):
                span.extract()
            meta["title"] = h1.get_text(strip=True) or None

        circle = soup.select_one("a.circleName__txt")
        if circle:
            meta["maker"] = circle.get_text(strip=True) or None

        # Spec rows: <dl class="informationList"><dt>label</dt><dd>value</dd>
        for dl in soup.select(".productInformation__item dl.informationList"):
            dt, dd = dl.find("dt"), dl.find("dd")
            if not dt or not dd:
                continue
            key = dt.get_text(strip=True)
            val = dd.get_text(" ", strip=True)
            if val in ("----", "-", ""):
                continue
            if key == "配信開始日":
                meta["release_date"] = normalize_date(val)
            elif key == "シリーズ":
                meta["series"] = val
            elif key in ("声優", "出演声優"):
                for part in re.split(r"[、,，/／\s]+", val):
                    part = part.strip()
                    if part and part not in meta["seiyuu"]:
                        meta["seiyuu"].append(part)
            elif key == "ジャンル":
                genres = [a.get_text(strip=True) for a in dd.find_all("a")]
                genres = [g for g in genres if g]
                if genres:
                    meta["extra"]["genres"] = genres
            elif key == "作品形式":
                meta["extra"]["work_type"] = val

        og = soup.select_one('meta[property="og:image"][content]')
        if og:
            meta["cover_url"] = og["content"]
        else:
            img = soup.select_one(".productPreview__item img[src]")
            if img:
                meta["cover_url"] = img["src"]

        # Prefer the circle-set price over a transient campaign price.
        price = soup.select_one(".priceList__sub--big") or soup.select_one("p.priceList__main")
        if price:
            m = re.search(r"[\d,]+", price.get_text(strip=True))
            if m:
                meta["price"] = m.group(0) + "円"

        desc_el = soup.select_one(".m-productSummary p.summary__txt")
        if desc_el:
            desc = desc_el.get_text("\n", strip=True)
            meta["description"] = desc or None
            for name in _CV_RE.findall(desc):
                for part in re.split(r"[、,，・]", name):
                    part = part.strip()
                    if part and part not in meta["seiyuu"]:
                        meta["seiyuu"].append(part)

        # Review average from JSON-LD if present (wrapped in //<![CDATA[ ... //]]>)
        for script in soup.select('script[type="application/ld+json"]'):
            text = re.sub(r"//\s*(?:<!\[CDATA\[|\]\]>)", "", script.string or "")
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data.get("@type") == "Product":
                rating = data.get("aggregateRating") or {}
                if isinstance(rating, dict) and rating.get("ratingValue"):
                    meta["extra"]["rating"] = (
                        f"{rating['ratingValue']} ({rating.get('ratingCount', '?')}件)"
                    )
                break

        return meta
