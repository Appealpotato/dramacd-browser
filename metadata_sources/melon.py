"""Melonbooks (melonbooks.co.jp) metadata source.

Doujin shop with strong circle/event metadata — covers physical doujin CDs
that never had a DLsite page. Product URLs:
    https://www.melonbooks.co.jp/detail/detail.php?product_id={numeric}
Search: GET /search/search.php?mode=search&...&name={query} (UTF-8).
Adult listings need the `AUTH_ADULT=1` consent cookie (static, no account).

AT-RISK: the site WAF returns 403 to non-browser clients from this network
(verified 2026-06, even with full browser headers / HTTP/2) — parser is
fixture-tested against Wayback-archived markup (2024-2025 era); live
fetches may need a JP network."""
import re
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup

from .base import CV_RE as _CV_RE
from .base import MetadataSource, SourceError, empty_metadata, normalize_date

BASE = "https://www.melonbooks.co.jp"

# Circle cell text carries a work-count suffix: "あまみどころ (作品数:24)"
_WORKS_COUNT_RE = re.compile(r"[\s ]*[（(]作品数\s*[:：]\s*\d+[)）]\s*$")


def _abs_url(url: str | None) -> str | None:
    """Melonbooks asset URLs are protocol-relative (//melonbooks.akamaized.net/...)."""
    if not url:
        return None
    if url.startswith("//"):
        return "https:" + url
    return urljoin(BASE, url)


class MelonbooksSource(MetadataSource):
    name = "melon"
    label = "Melonbooks"
    url_example = "https://www.melonbooks.co.jp/detail/detail.php?product_id=200090"
    supports_search = True
    _url_re = re.compile(r"melonbooks\.co\.jp/detail/detail\.php\?(?:[^#\s]*&)?product_id=(\d+)", re.I)
    cookies = {"AUTH_ADULT": "1"}  # adult consent gate

    # ------------------------------------------------------------- fetch
    async def fetch_by_url(self, client: httpx.AsyncClient, url: str) -> dict:
        m = self._url_re.search(url or "")
        if not m:
            raise SourceError(f"{self.label}: not a product URL: {url}")
        canonical = f"{BASE}/detail/detail.php?product_id={m.group(1)}"
        html = await self._get(client, canonical)
        return self.parse_product(html, canonical)

    async def search(self, client: httpx.AsyncClient, query: str) -> list[dict]:
        url = (f"{BASE}/search/search.php?mode=search&search_disp=&category_id=0"
               f"&text_type=&name={quote(query)}")
        html = await self._get(client, url)
        return self.parse_search(html)

    # ------------------------------------------------------------- parse
    def parse_product(self, html: str, source_url: str) -> dict:
        soup = BeautifulSoup(html, "html.parser")
        meta = empty_metadata(self.name, source_url)

        h1 = soup.select_one("h1.page-header")
        if h1:
            meta["title"] = h1.get_text(strip=True) or None

        # Spec table: th label / td value rows
        for tr in soup.select(".table-wrapper table tr"):
            th, td = tr.find("th"), tr.find("td")
            if not th or not td:
                continue
            key = th.get_text(strip=True)
            if key == "サークル名":
                a = td.find("a")
                if a:
                    meta["maker"] = _WORKS_COUNT_RE.sub("", a.get_text(strip=True)) or None
            elif key == "作家名":
                authors = [a.get_text(strip=True) for a in td.select("a[href*='text_type=author']")]
                authors = [a for a in authors if a]
                if authors:
                    meta["extra"]["author"] = ", ".join(authors)
            elif key == "ジャンル":
                genres = [a.get_text(strip=True) for a in td.find_all("a")]
                genres = [g for g in genres if g]
                if genres:
                    meta["extra"]["genres"] = genres
            elif key == "発行日":
                # publication date — fallback if no shop release date below
                if not meta["release_date"]:
                    meta["release_date"] = normalize_date(td.get_text(strip=True))
            elif key == "イベント":
                event = td.get_text(" ", strip=True)
                if event:
                    meta["extra"]["event"] = event
            elif key == "作品種別":
                val = td.get_text(strip=True)
                if val:
                    meta["extra"]["product_type"] = val
            elif key in ("声優", "出演声優"):
                for part in re.split(r"[、,，/／\s]+", td.get_text(" ", strip=True)):
                    part = part.strip()
                    if part and part not in meta["seiyuu"]:
                        meta["seiyuu"].append(part)
            elif key == "JANコード":
                val = td.get_text(strip=True)
                if re.fullmatch(r"\d{8,14}", val):
                    meta["jan"] = val

        # Shop release date beats 発行日 (event publication date)
        release = soup.select_one(".product-info__release-date")
        if release:
            date = normalize_date(release.get_text(strip=True))
            if date:
                meta["release_date"] = date

        img = soup.select_one(".slider figure a img[src], .my-gallery figure a img[src]")
        if img:
            meta["cover_url"] = _abs_url(img.get("data-src") or img["src"])
        if not meta["cover_url"] or "now_printing" in (meta["cover_url"] or ""):
            og = soup.select_one('meta[property="og:image"][content]')
            if og:
                meta["cover_url"] = _abs_url(og["content"])

        price = soup.select_one(".item-meta3 p.price") or soup.select_one("p.price .price--value")
        if price:
            m = re.search(r"[\d,]+", price.get_text(strip=True))
            if m:
                meta["price"] = m.group(0) + "円"

        # スタッフのオススメポイント blurb doubles as the description
        for h3 in soup.select("h3.page-headline"):
            if "オススメ" in h3.get_text() or "作品紹介" in h3.get_text():
                block = h3.find_next_sibling("div")
                if block:
                    desc = block.get_text("\n", strip=True)
                    if desc:
                        meta["description"] = desc
                        break

        if meta["description"]:
            for name in _CV_RE.findall(meta["description"]):
                for part in re.split(r"[、,，・]", name):
                    part = part.strip()
                    if part and part not in meta["seiyuu"]:
                        meta["seiyuu"].append(part)

        tags = [
            a.get_text(strip=True).lstrip("#")
            for a in soup.select(".item-detail2 a[href*='/tags/']")
            if a.get_text(strip=True)
        ]
        if tags:
            meta["extra"]["tags"] = list(dict.fromkeys(tags))

        notes = [s.get_text(strip=True) for s in soup.select(".item-notes")]
        if any("18禁" in n or "成年" in n for n in notes):
            meta["extra"]["age_rating"] = "R18"

        return meta

    def parse_search(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        hits = []
        for li in soup.select(".item-list ul > li"):
            a = li.select_one(".item-meta a[href*='detail.php?product_id=']")
            title = li.select_one(".item-ttl")
            if not a or not title or not title.get_text(strip=True):
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
            img = li.select_one(".item-thumbnail img")
            if img:
                thumb = img.get("data-src") or img.get("src")
                if thumb and "now_printing" not in thumb:
                    hit["thumbnail"] = _abs_url(thumb)
            price = li.select_one("p.item-price")
            if price:
                m = re.search(r"[\d,]+", price.get_text(strip=True))
                if m:
                    hit["price"] = m.group(0) + "円"
            state = li.select_one(".item-state")
            circle = li.select_one(".search-item-author-author a[href*='circle_id=']")
            parts = [el.get_text(strip=True) for el in (state, circle)
                     if el and el.get_text(strip=True)]
            if parts:
                hit["category"] = " / ".join(parts)
            hits.append(hit)
        return hits
