"""Stellaworth (stellaworth.co.jp) metadata source.

Otome/BLCD specialty shop known for original tokuten — the tokuten box is
the main reason to fetch from here. Shift_JIS-era PHP shop. Product URLs:
    https://www.stellaworth.co.jp/shop/item.php?item_id={alnum-id}
Search: GET /shop/search_result.php?name={query} — the query must be
Shift_JIS percent-encoded (the site predates UTF-8 forms)."""
import re
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup

from .base import CV_RE as _CV_RE
from .base import MetadataSource, SourceError, empty_metadata, normalize_date

BASE = "https://www.stellaworth.co.jp"

# Cast block lines: "・土方歳三 ：浪川大輔" (role first, actor after the colon)
_CAST_LINE_RE = re.compile(r"^[・･◆●○]?\s*([^：:]{1,30})\s*[：:]\s*(.+)$")
_CAST_HEADER_RE = re.compile(r"[【≪\[（(]?\s*(?:キャスト|ＣＡＳＴ|CAST|出演)\s*[】≫\]）)]?", re.I)


class StellaworthSource(MetadataSource):
    name = "stellaworth"
    label = "Stellaworth"
    url_example = "https://www.stellaworth.co.jp/shop/item.php?item_id=1nkCHY1d11d"
    supports_search = True
    _url_re = re.compile(r"stellaworth\.co\.jp/shop/item\.php\?(?:[^#\s]*&)?item_id=([A-Za-z0-9]+)", re.I)

    # ------------------------------------------------------------- fetch
    async def fetch_by_url(self, client: httpx.AsyncClient, url: str) -> dict:
        m = self._url_re.search(url or "")
        if not m:
            raise SourceError(f"{self.label}: not an item URL: {url}")
        canonical = f"{BASE}/shop/item.php?item_id={m.group(1)}"
        html = await self._get(client, canonical)
        return self.parse_product(html, canonical)

    async def search(self, client: httpx.AsyncClient, query: str) -> list[dict]:
        q = quote(query.encode("shift_jis", errors="ignore"))
        url = (f"{BASE}/shop/search_result.php?item=&name={q}&lc=&sc=&br=&kw="
               f"&al=0&ryf=&rmf=&rdf=&ryt=&rmt=&rdt=&pl=&ei=0&dt=0&pageID=1")
        html = await self._get(client, url)
        return self.parse_search(html)

    # ------------------------------------------------------------- parse
    def parse_product(self, html: str, source_url: str) -> dict:
        soup = BeautifulSoup(html, "html.parser")
        meta = empty_metadata(self.name, source_url)

        intro = soup.select_one("article.itemDetailIntro") or soup

        h1 = intro.select_one("h1")
        if h1:
            meta["title"] = h1.get_text(strip=True)

        # Spec table: 商品ID / JANコード / 種別 / 商品名 / メーカー / 発売日 / 販売価格
        for tr in intro.select("table tr"):
            th, td = tr.find("th"), tr.find("td")
            if not th or not td:
                continue
            key = th.get_text(strip=True)
            val = td.get_text(" ", strip=True)
            if key == "JANコード" and re.fullmatch(r"\d{8,14}", val):
                meta["jan"] = val
            elif key == "メーカー":
                meta["maker"] = val or None
            elif key == "発売日":
                meta["release_date"] = normalize_date(val)  # 未定 -> None
            elif key == "販売価格":
                meta["price"] = val or None  # "税抜￥4,000（税込￥4,400）"
            elif key == "種別":
                cats = [a.get_text(strip=True) for a in td.find_all("a")]
                if cats:
                    meta["extra"]["category"] = " / ".join(cats)
            elif key == "商品ID":
                meta["extra"]["item_id"] = val

        img = intro.select_one("h1 ~ span img[src], span img[src]")
        if img and "noimg" not in img["src"]:
            meta["cover_url"] = urljoin(source_url, img["src"])

        # Tokuten box: <strong>ステラワース特典</strong> + bonus text
        tokuten = []
        for p in intro.select(".specialInfo dd p"):
            label = p.find("strong")
            label_text = label.get_text(strip=True) if label else ""
            if label:
                label.extract()
            body = p.get_text(" ", strip=True)
            text = f"{label_text}: {body}" if label_text and body else (label_text or body)
            if text:
                tokuten.append(text)
        if tokuten:
            meta["extra"]["tokuten"] = list(dict.fromkeys(tokuten))

        desc_el = intro.select_one(".txtNormal")
        if desc_el:
            desc = desc_el.get_text("\n", strip=True)
            meta["description"] = desc or None
            meta["seiyuu"] = self._extract_cast(desc)

        return meta

    @staticmethod
    def _extract_cast(desc: str) -> list[str]:
        """Actors from a 【キャスト】 block of "・役名：声優" lines, plus any
        plain CV credits elsewhere in the text."""
        seiyuu: list[str] = []
        in_cast = False
        for line in desc.splitlines():
            line = line.strip()
            if _CAST_HEADER_RE.fullmatch(line):
                in_cast = True
                continue
            if not in_cast:
                continue
            m = _CAST_LINE_RE.match(line)
            if not m:
                if line:  # non-matching non-blank line ends the block
                    in_cast = False
                continue
            actor = re.sub(r"[（(].*?[)）]", "", m.group(2)).strip()
            if actor and actor not in seiyuu:
                seiyuu.append(actor)
        for name in _CV_RE.findall(desc):
            for part in re.split(r"[、,，・]", name):
                part = part.strip()
                if part and part not in seiyuu:
                    seiyuu.append(part)
        return seiyuu

    def parse_search(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        hits = []
        for li in soup.select("ul.listItem > li"):
            a = li.select_one("dt strong a[href*='item.php']")
            if not a or not a.get_text(strip=True):
                continue
            hit = {
                "source": self.name,
                "title": a.get_text(strip=True),
                "url": urljoin(f"{BASE}/shop/", a["href"]),
                "thumbnail": None,
                "release_date": None,
                "price": None,
                "category": None,
            }
            img = li.select_one("dt span img[src]")
            if img and "noimg" not in img["src"]:
                hit["thumbnail"] = urljoin(f"{BASE}/shop/", img["src"])
            cat = li.select_one("dt em")
            if cat and cat.get_text(strip=True):
                hit["category"] = cat.get_text(strip=True)
            maker = None
            for p in li.select("dd p"):
                text = p.get_text(" ", strip=True)
                if text.startswith("発売日"):
                    hit["release_date"] = normalize_date(text)
                elif text.startswith("価格"):
                    hit["price"] = text.split("：", 1)[-1].strip() or None
                elif text.startswith("メーカー"):
                    maker = text.split("：", 1)[-1].strip() or None
            if maker:
                hit["category"] = f"{hit['category']} / {maker}" if hit["category"] else maker
            hits.append(hit)
        return hits
