"""Rejet (rejet.jp) metadata source — otome/drama CD maker's own works DB.

Unlike Gamers/Chil-Chil there are NO per-work detail pages: works render as
inline <article class="item"> blocks on paginated WordPress listing pages
(/works/?cat=N, /works/?s=query). Each block carries title (with CV credit),
cover (filename embeds the catalog number, e.g. REC-861_500.jpg), 発売日,
price, series category, and the blurb.

To give every work an addressable URL anyway, hits are keyed with a
"#rejet=<catalog-or-title-hash>" fragment appended to the page URL;
fetch_by_url re-fetches that page and picks the matching block. A pasted
listing URL without a fragment only resolves when the page holds exactly
one work.

Caveat: WordPress search is literal — series abbreviations (ディアラバ) and
unspaced cast names (日野聡 vs 日野 聡) return nothing."""
import hashlib
import re
from urllib.parse import quote, urldefrag, urljoin

import httpx
from bs4 import BeautifulSoup

from .base import CV_RE, MetadataSource, SourceError, empty_metadata, normalize_date

BASE = "https://rejet.jp"

# Catalog number from cover filenames: .../2019/09/REC-861_500.jpg
_CATALOG_RE = re.compile(r"/([A-Z]{2,5}-\d+)[_.]")


class RejetSource(MetadataSource):
    name = "rejet"
    label = "Rejet"
    url_example = "https://rejet.jp/works/?cat=41"
    supports_search = True
    _url_re = re.compile(r"rejet\.jp/works/?", re.I)

    # ------------------------------------------------------------- fetch
    async def fetch_by_url(self, client: httpx.AsyncClient, url: str) -> dict:
        page_url, fragment = urldefrag(url or "")
        if not self._url_re.search(page_url):
            raise SourceError(f"{self.label}: not a works URL: {url}")
        key = None
        m = re.match(r"rejet=(.+)", fragment or "")
        if m:
            key = m.group(1)
        html = await self._get(client, page_url)
        works = self.parse_page(html, page_url)
        if not works:
            raise SourceError(f"{self.label}: no works found on {page_url}")
        if key:
            for work_key, meta in works:
                if work_key == key:
                    return meta
            raise SourceError(f"{self.label}: work '{key}' not found on {page_url}")
        if len(works) == 1:
            return works[0][1]
        raise SourceError(
            f"{self.label}: listing holds {len(works)} works — search by title "
            "and pick one instead of pasting the listing URL"
        )

    async def search(self, client: httpx.AsyncClient, query: str) -> list[dict]:
        page_url = f"{BASE}/works/?s={quote(query)}"
        html = await self._get(client, page_url)
        hits = []
        for key, meta in self.parse_page(html, page_url):
            hits.append({
                "source": self.name,
                "title": meta["title"],
                "url": meta["source_url"],
                "thumbnail": meta["cover_url"],
                "release_date": meta["release_date"],
                "price": meta["price"],
                "category": meta["series"] or meta["extra"].get("type"),
            })
        return hits

    # ------------------------------------------------------------- parse
    def parse_page(self, html: str, page_url: str) -> list[tuple[str, dict]]:
        """All works on a listing/search page as (key, normalized-meta)."""
        soup = BeautifulSoup(html, "html.parser")
        works: list[tuple[str, dict]] = []
        for article in soup.select("article.item"):
            h2 = article.select_one(".information h2")
            if not h2 or not h2.get_text(strip=True):
                continue
            meta = empty_metadata(self.name, page_url)
            meta["title"] = h2.get_text(strip=True)
            meta["maker"] = "Rejet"

            img = article.select_one("img.worksimg[src]")
            if img:
                # Site still serves http:// asset URLs; normalize.
                meta["cover_url"] = urljoin(BASE, img["src"]).replace(
                    "http://rejet.jp", "https://rejet.jp"
                )
                cm = _CATALOG_RE.search(meta["cover_url"])
                if cm:
                    meta["catalog_number"] = cm.group(1)

            date = article.select_one(".information p.date")
            if date:
                meta["release_date"] = normalize_date(date.get_text(strip=True))

            # Yes, the site's CSS class really is "plice".
            price = article.select_one(".information p.plice")
            if price:
                text = price.get_text(strip=True)
                text = re.sub(r"^価格\s*[:：]\s*", "", text)
                if text and "円" not in text:
                    text = re.sub(r"^([\d,]+)", r"\1円", text)
                meta["price"] = text or None

            cate = article.select_one(".information p.cate a[rel='category']") \
                or article.select_one(".information p.cate a")
            if cate and cate.get_text(strip=True):
                meta["series"] = cate.get_text(strip=True)

            text_div = article.select_one(".information .text")
            if text_div:
                desc = text_div.get_text("\n", strip=True).replace("\xa0", "").strip()
                meta["description"] = desc or None

            for cv in CV_RE.findall(meta["title"]):
                cv = cv.strip()
                if cv and cv not in meta["seiyuu"]:
                    meta["seiyuu"].append(cv)

            type_el = article.select_one(".information p.type")
            if type_el and type_el.get_text(strip=True):
                meta["extra"]["type"] = type_el.get_text(strip=True)
            official = article.select_one(".images .links a[href]")
            if official:
                meta["extra"]["official_site"] = official["href"]

            key = meta["catalog_number"] or "t-" + hashlib.md5(
                meta["title"].encode("utf-8")
            ).hexdigest()[:10]
            meta["source_url"] = f"{page_url}#rejet={key}"
            works.append((key, meta))
        return works
