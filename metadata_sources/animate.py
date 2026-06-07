"""animate online shop (animate-onlineshop.jp) metadata source.

Same corporate group as Gamers but a different storefront build — selectors
diverge enough (verified 2026-06) that this is a sibling implementation,
not a GamersSource subclass. Product URLs:
    https://www.animate-onlineshop.jp/pd/{numeric}/
    https://www.animate-onlineshop.jp/pn/{urlencoded-title}/pd/{numeric}/
Search: GET /products/list.php?mode=search&smt={keywords}"""
import re
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup

from .base import CV_RE as _CV_RE
from .base import MetadataSource, SourceError, empty_metadata, normalize_date

BASE = "https://www.animate-onlineshop.jp"

# Cast line inside ≪キャスト≫: "林 壮馬役 阿座上洋平 吉鷹 南役 坂田将吾 他"
_ROLE_CAST_RE = re.compile(r"役\s*[:：]?\s*([^\s　、,／/]+)")


class AnimateSource(MetadataSource):
    name = "animate"
    label = "animate"
    url_example = "https://www.animate-onlineshop.jp/pd/3465774/"
    supports_search = True
    _url_re = re.compile(r"animate-onlineshop\.jp/(?:pn/[^/]+/)?pd/(\d+)/?", re.I)

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

        h1 = soup.select_one(".item_overview_detail h1") or soup.select_one("h1")
        if h1:
            meta["title"] = h1.get_text(strip=True)

        # Cover: the gallery image is full-size; og:image carries forced
        # 1200x630 resize params.
        img = soup.select_one(".item_images .item_image_selected img[src]")
        if img:
            meta["cover_url"] = urljoin(source_url, img["src"])
        else:
            og = soup.select_one('meta[property="og:image"][content]')
            if og:
                meta["cover_url"] = og["content"]

        price = soup.select_one(".item_price p.price")
        if price:
            m = re.search(r"[\d,]+", price.get_text(strip=True))
            if m:
                meta["price"] = m.group(0) + "円"

        release = soup.select_one(".item_status p.release")
        if release:
            meta["release_date"] = normalize_date(release.get_text(" ", strip=True))

        stock = soup.select_one(".item_status p.stock")
        if stock:
            avail = stock.get_text(" ", strip=True)
            if avail:
                meta["extra"]["availability"] = avail

        # 品番 / JAN sit in free text ("品番：CRWS-119", "JANコード：456...")
        m = re.search(r"品番\s*[:：]\s*([A-Za-z0-9][A-Za-z0-9\-]+)", html)
        if m:
            meta["catalog_number"] = m.group(1)
        m = re.search(r"JANコード\s*[:：]\s*(\d{8,14})", html)
        if m:
            meta["jan"] = m.group(1)

        info = soup.select_one("#item_productinfo .detail_info") or soup.select_one(".detail_info")
        if info:
            desc = info.get_text("\n", strip=True)
            desc = re.sub(r"関連ワード\s*[:：].*$", "", desc, flags=re.S).strip()
            meta["description"] = desc or None
            meta["seiyuu"] = self._extract_cast(desc)

        # Store/maker bonus blurbs (特典について)
        tokuten = [
            h3.get_text(strip=True)
            for h3 in soup.select("section.item_benefit .detail h3")
            if h3.get_text(strip=True)
        ]
        if tokuten:
            meta["extra"]["tokuten"] = list(dict.fromkeys(tokuten))

        # 関連語句 labels double as keywords (cast names, work title)
        keywords = [
            s.get_text(strip=True)
            for s in soup.select(".items_label ul li a span")
            if s.get_text(strip=True)
        ]
        if keywords:
            meta["extra"]["keywords"] = list(dict.fromkeys(keywords))

        return meta

    @staticmethod
    def _extract_cast(desc: str) -> list[str]:
        """Actors from a ≪キャスト≫ block ("役名役 声優名 ..." pairs), plus
        plain CV credits."""
        seiyuu: list[str] = []
        in_cast = False
        for line in desc.splitlines():
            line = line.strip()
            if re.fullmatch(r"[【≪\[（(]?\s*(?:キャスト|ＣＡＳＴ|CAST|出演者?)\s*[】≫\]）)]?", line, re.I):
                in_cast = True
                continue
            if not in_cast:
                continue
            names = _ROLE_CAST_RE.findall(line)
            if not names:
                if line:
                    in_cast = False
                continue
            for name in names:
                name = name.strip()
                if name and name != "他" and name not in seiyuu:
                    seiyuu.append(name)
        for name in _CV_RE.findall(desc):
            for part in re.split(r"[、,，・]", name):
                part = part.strip()
                if part and part not in seiyuu:
                    seiyuu.append(part)
        return seiyuu

    def parse_search(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        hits = []
        for li in soup.select("div.item_list ul > li"):
            a = li.select_one('h3 a[href*="/pd/"]')
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
            img = li.select_one(".item_list_thumb img[src]")
            if img:
                hit["thumbnail"] = img["src"]
            price = li.select_one(".item_list_detail p.price")
            if price:
                text = price.get_text(strip=True)
                m = re.search(r"[\d,]+", text)
                if m:
                    hit["price"] = m.group(0) + "円"
            release = li.select_one(".item_list_detail p.release")
            if release:
                hit["release_date"] = normalize_date(release.get_text(strip=True))
            cat = li.select_one(".item_list_detail p.media a")
            if cat and cat.get_text(strip=True):
                hit["category"] = cat.get_text(strip=True)
            hits.append(hit)
        return hits
