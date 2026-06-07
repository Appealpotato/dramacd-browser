"""DiGiket (digiket.com) metadata source.

Long-running doujin download shop — carries voice/drama works from the
pre-DLsite-monopoly era, so it can rescue metadata for delisted works.
Shift_JIS-era site. Product URLs:
    https://www.digiket.com/work/show/_data/ID=ITM{7 digits}/
Search: GET /result/_data/A={query}/ — the query must be Shift_JIS
percent-encoded (UTF-8 queries silently return 0 hits)."""
import re
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup

from .base import CV_RE as _CV_RE
from .base import MetadataSource, SourceError, empty_metadata, normalize_date

BASE = "https://www.digiket.com"


class DigiketSource(MetadataSource):
    name = "digiket"
    label = "DiGiket"
    url_example = "https://www.digiket.com/work/show/_data/ID=ITM0337679/"
    supports_search = True
    _url_re = re.compile(r"digiket\.com/work/show/_data/ID=(ITM\d+)", re.I)

    # ------------------------------------------------------------- fetch
    async def fetch_by_url(self, client: httpx.AsyncClient, url: str) -> dict:
        m = self._url_re.search(url or "")
        if not m:
            raise SourceError(f"{self.label}: not a work URL: {url}")
        canonical = f"{BASE}/work/show/_data/ID={m.group(1).upper()}/"
        html = await self._get(client, canonical)
        return self.parse_product(html, canonical)

    async def search(self, client: httpx.AsyncClient, query: str) -> list[dict]:
        q = quote(query.encode("shift_jis", errors="ignore"))
        html = await self._get(client, f"{BASE}/result/_data/A={q}/")
        return self.parse_search(html)

    # ------------------------------------------------------------- parse
    def parse_product(self, html: str, source_url: str) -> dict:
        soup = BeautifulSoup(html, "html.parser")
        meta = empty_metadata(self.name, source_url)

        h1 = soup.select_one("h1.works-page-title") or soup.select_one("h1.headline")
        if h1:
            meta["title"] = h1.get_text(strip=True) or None

        # Info rows: <dd><div class="sub">label：</div><div class="sub-data">value</div>
        # The spec section lower down uses sub2/sub-data2 with the same shape.
        for dd in soup.select("dl.maker dd, dl dd"):
            label_el = dd.select_one(".sub, .sub2")
            value_el = dd.select_one(".sub-data, .sub-data2")
            if not label_el or not value_el:
                continue
            key = label_el.get_text(strip=True).rstrip("：:")
            val = value_el.get_text(" ", strip=True)
            if not val:
                continue
            if key == "サークル":
                meta["maker"] = val
            elif key in ("声優", "出演声優", "CV"):
                for part in re.split(r"[、,，/／\s]+", val):
                    part = part.strip()
                    if part and part not in meta["seiyuu"]:
                        meta["seiyuu"].append(part)
            elif key in ("登録日", "配信開始日", "発売日"):
                if not meta["release_date"]:
                    meta["release_date"] = normalize_date(val)
            elif key == "ジャンル":
                meta["extra"]["work_type"] = val
            elif key == "キー":
                tags = [a.get_text(strip=True) for a in value_el.find_all("a")]
                tags = [t for t in tags if t]
                if tags:
                    meta["extra"]["tags"] = tags
            elif key == "シリーズ":
                meta["series"] = val

        # Cover: first carousel slide is the full package image; og:image is
        # a cropped share rendition.
        img = soup.select_one("#syokai_carousel .item img[src]")
        if img:
            meta["cover_url"] = urljoin(source_url, img["src"])
        else:
            og = soup.select_one('meta[property="og:image"][content]')
            if og:
                meta["cover_url"] = og["content"]

        price = soup.select_one(".work-price .price-value")
        if price:
            text = price.get_text(strip=True)
            meta["price"] = text if text.endswith("円") else text + "円"

        desc_el = soup.select_one(".works-desc")
        if desc_el:
            desc = desc_el.get_text("\n", strip=True)
            meta["description"] = desc or None

        # CV credits often live in the title (【CV.安野希世乃】) or description
        for text in filter(None, (meta["title"], meta["description"])):
            for name in _CV_RE.findall(text):
                for part in re.split(r"[、,，・]", name):
                    part = part.strip()
                    if part and part not in meta["seiyuu"]:
                        meta["seiyuu"].append(part)

        return meta

    def parse_search(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        hits = []
        for box in soup.select("div.item-box"):
            a = box.select_one("dt.item_name a[href]")
            if not a or not a.get_text(strip=True):
                continue
            hit = {
                "source": self.name,
                "title": a.get_text(strip=True),
                "url": urljoin(BASE, a["href"]),
                "thumbnail": None,
                "release_date": None,
                "price": None,
                "category": None,
            }
            img = box.select_one(".item-thum img")
            if img:
                thumb = img.get("data-original") or img.get("src")
                if thumb and "1px" not in thumb:
                    hit["thumbnail"] = urljoin(BASE, thumb)
            price = box.select_one(".item-price strong")
            if price:
                text = price.get_text(strip=True)
                hit["price"] = text if text.endswith("円") else text + "円"
            genre = box.select_one(".item_genre")
            circle = box.select_one("dd.item_circle a")
            parts = [el.get_text(strip=True) for el in (genre, circle)
                     if el and el.get_text(strip=True)]
            if parts:
                hit["category"] = " / ".join(parts)
            hits.append(hit)
        return hits
