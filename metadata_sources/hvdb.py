"""HVDB (hvdb.me) metadata source.

Community database of DLsite voice works keyed by RJ code — its value here
is **English titles** (and romanized CV names) for works the JP-only
sources can't translate. Work URLs:
    https://hvdb.me/Dashboard/Details/{rj-digits}
Search accepts an RJ code only ("RJ01644019" or bare digits) — the site
has no public text-search endpoint, just an RJ-code lookup box.

Emits `extra["product_code"]` so the apply layer's adopt-code path can
promote a manual item to its real RJ code."""
import re
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .base import MetadataSource, SourceError, empty_metadata

BASE = "https://hvdb.me"

_RJ_QUERY_RE = re.compile(r"^\s*(?:RJ)?(\d{6,8})\s*$", re.I)


class HvdbSource(MetadataSource):
    name = "hvdb"
    label = "HVDB"
    url_example = "https://hvdb.me/Dashboard/Details/01644019"
    supports_search = True  # RJ-code lookup only
    _url_re = re.compile(r"hvdb\.me/(?:Dashboard|Dramas)/Details/(\d+)", re.I)

    # ------------------------------------------------------------- fetch
    async def fetch_by_url(self, client: httpx.AsyncClient, url: str) -> dict:
        m = self._url_re.search(url or "")
        if not m:
            raise SourceError(f"{self.label}: not a work URL: {url}")
        canonical = f"{BASE}/Dashboard/Details/{m.group(1)}"
        html = await self._get(client, canonical)
        return self.parse_product(html, canonical)

    async def search(self, client: httpx.AsyncClient, query: str) -> list[dict]:
        """RJ-code lookup: non-code queries return no hits (HVDB has no
        text search)."""
        m = _RJ_QUERY_RE.match(query or "")
        if not m:
            return []
        canonical = f"{BASE}/Dashboard/Details/{m.group(1)}"
        try:
            meta = self.parse_product(await self._get(client, canonical), canonical)
        except SourceError:
            return []  # unknown code -> empty result, not an error banner
        if not meta["title"] and not meta["title_en"]:
            return []
        return [{
            "source": self.name,
            "title": meta["title"] or meta["title_en"],
            "url": canonical,
            "thumbnail": meta["cover_url"],
            "release_date": None,
            "price": None,
            "category": meta["title_en"] or meta["extra"].get("product_code"),
        }]

    # ------------------------------------------------------------- parse
    def parse_product(self, html: str, source_url: str) -> dict:
        soup = BeautifulSoup(html, "html.parser")
        meta = empty_metadata(self.name, source_url)

        name = soup.select_one("input#Name[value]")
        if name and name["value"].strip():
            meta["title"] = name["value"].strip()

        eng = soup.select_one("input#EngName[value]")
        if eng and eng["value"].strip():
            meta["title_en"] = eng["value"].strip()

        # "Work Details - RJ01644019" -> RJ code for the adopt-code path
        h2 = soup.find("h2")
        if h2:
            m = re.search(r"\b(RJ\d{6,8})\b", h2.get_text())
            if m:
                meta["extra"]["product_code"] = m.group(1)

        # Circle renders as "JP名 / EN name" inside one anchor
        circle = soup.select_one("a.detailCircle")
        if circle:
            parts = [p.strip() for p in circle.get_text().split("/") if p.strip()]
            if parts:
                meta["maker"] = parts[0]
                if len(parts) > 1 and parts[1] != parts[0]:
                    meta["extra"]["circle_en"] = parts[1]

        cvs = soup.select_one("input#CVsString[value]")
        if cvs:
            for part in cvs["value"].split(","):
                part = part.strip()
                if part and part != "N/A" and part not in meta["seiyuu"]:
                    meta["seiyuu"].append(part)

        tags = soup.select_one("input#TagsString[value]")
        if tags:
            tag_list = [t.strip() for t in tags["value"].split(",")
                        if t.strip() and t.strip() != "N/A"]
            if tag_list:
                meta["extra"]["tags"] = tag_list

        img = soup.select_one("img.detailImage[src]")
        if img:
            meta["cover_url"] = urljoin(source_url, img["src"])

        sfw = soup.select_one('input#SFW[type="checkbox"]')
        if sfw is not None:
            meta["extra"]["sfw"] = sfw.has_attr("checked")

        return meta
