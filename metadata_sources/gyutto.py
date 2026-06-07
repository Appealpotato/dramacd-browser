"""Gyutto (gyutto.com) metadata source.

Legacy doujin download shop (EUC-JP era) — another rescue source for
voice/drama works delisted elsewhere. Product URLs:
    https://gyutto.com/i/item{numeric}
Search: GET /search/search_list.php?mode=search&search_keyword={query} —
the query must be EUC-JP percent-encoded (UTF-8 silently returns 0 hits).
Note: the product page renders the price via AJAX, so `price` stays None
on fetches; search result cards do carry prices."""
import re
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup

from .base import CV_RE as _CV_RE
from .base import MetadataSource, SourceError, empty_metadata, normalize_date

BASE = "https://gyutto.com"


class GyuttoSource(MetadataSource):
    name = "gyutto"
    label = "Gyutto"
    url_example = "https://gyutto.com/i/item282706"
    supports_search = True
    _url_re = re.compile(r"gyutto\.com/i/item(\d+)", re.I)

    # ------------------------------------------------------------- fetch
    async def fetch_by_url(self, client: httpx.AsyncClient, url: str) -> dict:
        m = self._url_re.search(url or "")
        if not m:
            raise SourceError(f"{self.label}: not an item URL: {url}")
        canonical = f"{BASE}/i/item{m.group(1)}"
        html = await self._get(client, canonical)
        return self.parse_product(html, canonical)

    async def search(self, client: httpx.AsyncClient, query: str) -> list[dict]:
        q = quote(query.encode("euc_jp", errors="ignore"))
        url = f"{BASE}/search/search_list.php?mode=search&search_keyword={q}"
        html = await self._get(client, url)
        return self.parse_search(html)

    # ------------------------------------------------------------- parse
    def parse_product(self, html: str, source_url: str) -> dict:
        soup = BeautifulSoup(html, "html.parser")
        meta = empty_metadata(self.name, source_url)

        h1 = soup.select_one(".parts_Mds01 h1") or soup.select_one("h1")
        if h1:
            meta["title"] = h1.get_text(strip=True).replace("\xa0", " ").strip() or None

        # Info rows: <dl class="BasicInfo"><dt>label</dt><dd>value</dd>
        for dl in soup.select("dl.BasicInfo"):
            dt, dd = dl.find("dt"), dl.find("dd")
            if not dt or not dd:
                continue
            key = dt.get_text(strip=True)
            if key == "サークル":
                a = dd.find("a")
                if a and a.get_text(strip=True):
                    meta["maker"] = a.get_text(strip=True)
            elif key in ("配信開始日", "発売日"):
                if not meta["release_date"]:
                    meta["release_date"] = normalize_date(dd.get_text(strip=True))
            elif key == "ジャンル":
                genres = [a.get_text(strip=True) for a in dd.find_all("a")]
                genres = [g for g in genres if g]
                if genres:
                    meta["extra"]["genres"] = genres
            elif key in ("カテゴリー", "カテゴリ"):
                cats = [a.get_text(strip=True) for a in dd.find_all("a")]
                cats = [c for c in cats if c]
                if cats:
                    meta["extra"]["category"] = " / ".join(cats)
            elif key == "作品形式":
                val = dd.get_text(" ", strip=True)
                if val:
                    meta["extra"]["work_type"] = val
            elif key == "年齢区分":
                val = dd.get_text(strip=True)
                if val:
                    meta["extra"]["age_rating"] = val
            elif key in ("声優", "出演声優", "CV"):
                for part in re.split(r"[、,，/／\s]+", dd.get_text(" ", strip=True)):
                    part = part.strip()
                    if part and part not in meta["seiyuu"]:
                        meta["seiyuu"].append(part)

        og = soup.select_one('meta[property="og:image"][content]')
        if og:
            # og:image comes as "http://Gyutto.com/..." — normalize host/scheme
            meta["cover_url"] = re.sub(r"^http://gyutto\.com", "https://gyutto.com",
                                       og["content"], flags=re.I)
        else:
            img = soup.select_one(".ItemPh img[src]")
            if img:
                meta["cover_url"] = urljoin(source_url, img["src"])

        # Lead blurb + full synopsis
        parts = []
        lead = soup.select_one(".unit_DetailLead")
        if lead:
            parts.append(lead.get_text("\n", strip=True))
        summary = soup.select_one(".unit_DetailSummary")
        if summary:
            text = summary.get_text("\n", strip=True)
            if text and text not in parts:
                parts.append(text)
        if parts:
            meta["description"] = "\n\n".join(dict.fromkeys(parts)) or None

        if meta["description"]:
            for name in _CV_RE.findall(meta["description"]):
                for part in re.split(r"[、,，・]", name):
                    part = part.strip()
                    if part and part not in meta["seiyuu"]:
                        meta["seiyuu"].append(part)

        return meta

    def parse_search(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        hits = []
        for box in soup.select("dl.ItemBox"):
            cell = box.select_one("dd.DefiPhotoName")
            a = cell.select_one("a[href*='/i/item']") if cell else None
            if not a:
                continue
            img = cell.select_one("img")
            # The anchor text is a truncated title; the img alt carries the
            # full one.
            title = (img.get("alt", "").strip() if img else "") or a.get_text(strip=True)
            if not title:
                continue
            hit = {
                "source": self.name,
                "title": title,
                "url": urljoin(BASE, a["href"]),
                "thumbnail": urljoin(BASE, img["src"]) if img and img.get("src") else None,
                "release_date": None,
                "price": None,
                "category": None,
            }
            price = box.select_one("dd.DefiPrice")
            if price and price.get_text(strip=True):
                text = price.get_text(strip=True)
                hit["price"] = text if text.endswith("円") else text + "円"
            circle = box.select_one("dd.DefiAuthor a")
            if circle and circle.get_text(strip=True):
                hit["category"] = circle.get_text(strip=True)
            hits.append(hit)
        return hits
