"""CAnDY BIBInBA (candybibinba.com) metadata source.

CAnDY BIBInBA is a BL / situation drama-CD label. Its official site carries a
full product page per CD — actually richer than the storefront it sells through
(complete track list, character bios, staff credits, series) — but it does NOT
sell directly: every page links out to Pocket Drama CD (pokedora.com) as the
real store. We capture that cross-link under ``extra['pokedora_url']`` so the
Pokedora source can be used to adopt the same release.

Product URLs are slug-based:
    https://candybibinba.com/product/{slug}/

Release date and price aren't on the page (they live on the Pokedora listing,
which is itself client-rendered) so both stay None — consistent with
PokedoraSource. The page is a plain server-rendered theme with stable BEM class
hooks: httpx-friendly, no JS, no age gate on the product pages.
"""
import re
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .base import MetadataSource, SourceError, empty_metadata

BASE = "https://candybibinba.com"

# Staff-row labels that identify the label/maker rather than a credited person.
_MAKER_LABELS = ("企画", "原作", "レーベル", "メーカー", "ブランド")
# Separators inside a cast / staff value.
_NAME_SEP_RE = re.compile(r"[、，,・/／]")
# Filler tokens ("and others") that are not names.
_FILLER_NAMES = {"他", "ほか", "その他", "etc", "etc.", "…"}


class CandyBibinbaSource(MetadataSource):
    name = "candybibinba"
    label = "CAnDY BIBInBA"
    url_example = "https://candybibinba.com/product/bodyescape5/"
    supports_search = False  # no on-site search index; paste-URL only
    _url_re = re.compile(r"candybibinba\.com/product/([A-Za-z0-9][A-Za-z0-9_-]*)/?", re.I)

    # ------------------------------------------------------------- fetch
    async def fetch_by_url(self, client: httpx.AsyncClient, url: str) -> dict:
        m = self._url_re.search(url or "")
        if not m:
            raise SourceError(f"{self.label}: not a product URL: {url}")
        canonical = f"{BASE}/product/{m.group(1)}/"
        html = await self._get(client, canonical)
        return self.parse_product(html, canonical)

    # ------------------------------------------------------------- parse
    def parse_product(self, html: str, source_url: str) -> dict:
        soup = BeautifulSoup(html, "html.parser")
        meta = empty_metadata(self.name, source_url)

        # Title: the h1 holds the clean title (no cast bracket). og:title tacks
        # on 【出演声優…】 | <site>, so fall back to its leading segment.
        h1 = soup.select_one(".product__title")
        if h1 and h1.get_text(strip=True):
            meta["title"] = h1.get_text(" ", strip=True)
        else:
            og = soup.find("meta", property="og:title")
            if og and og.get("content"):
                head = re.split(r"[|｜]", og["content"])[0]
                head = re.sub(r"【[^】]*】\s*$", "", head).strip()
                meta["title"] = head or None

        # Cover: og:image is the absolute jacket URL (with a ?<date> cachebuster).
        og_image = soup.find("meta", property="og:image")
        if og_image and og_image.get("content"):
            meta["cover_url"] = urljoin(BASE, og_image["content"].strip())

        # Series header, e.g. "BODY ESCAPEシリーズ" -> drop the trailing シリーズ.
        series = soup.select_one(".product__series")
        if series and series.get_text(strip=True):
            text = re.sub(r"シリーズ\s*$", "", series.get_text(" ", strip=True)).strip()
            meta["series"] = text or None

        # Synopsis / catchphrase block; fall back to og:description.
        intro = soup.select_one(".product__intro")
        if intro and intro.get_text(strip=True):
            meta["description"] = intro.get_text("\n", strip=True) or None
        if not meta["description"]:
            og_desc = soup.find("meta", property="og:description")
            if og_desc and og_desc.get("content"):
                meta["description"] = og_desc["content"].strip() or None

        # Staff rows: label/value pairs. CAST feeds seiyuu, 企画・原作 the maker,
        # everything else is credited under extra['staff'].
        seiyuu: list[str] = []
        staff: dict[str, str] = {}
        for li in soup.select(".product__staff li"):
            lab_el = li.select_one(".product__staff__label")
            val_el = li.select_one(".product__staff__text")
            if not lab_el or not val_el:
                continue
            label = lab_el.get_text(" ", strip=True)
            value = val_el.get_text(" ", strip=True)
            if not label or not value:
                continue
            if label.upper() == "CAST" or "声優" in label or "出演" in label:
                seiyuu.extend(self._split_names(value))
            elif any(k in label for k in _MAKER_LABELS):
                staff[label] = value
                if not meta["maker"]:
                    meta["maker"] = value
            else:
                staff[label] = value

        # Fallback cast line "出演声優：テトラポット登" if no CAST staff row existed.
        if not seiyuu:
            cast = soup.select_one(".product__cast")
            if cast:
                txt = re.sub(r"^[^:：]*[:：]", "", cast.get_text(" ", strip=True))
                seiyuu.extend(self._split_names(txt))

        # Character bios: dedicated name / cv / intro sub-blocks. The cv block is
        # a clean "cv.<name>" so we can mine it for seiyuu without grabbing bio.
        characters: list[str] = []
        for li in soup.select("li.characterlist__item"):
            name = self._text(li.select_one(".characterlist__name"))
            cv = self._text(li.select_one(".characterlist__cv"))
            intro = self._text(li.select_one(".characterlist__intro"))
            if cv:
                seiyuu.extend(self._split_names(re.sub(r"^cv[.．:：]?\s*", "", cv, flags=re.I)))
            parts = [p for p in (name, cv, intro) if p]
            if parts:
                characters.append(" / ".join(parts))

        meta["seiyuu"] = list(dict.fromkeys(n for n in seiyuu if n))
        if staff:
            meta["extra"]["staff"] = staff
        if characters:
            meta["extra"]["characters"] = characters

        # Track list: "<no>. <title>".
        tracks: list[str] = []
        for li in soup.select("li.tracklist__item"):
            no = self._text(li.select_one(".tracklist__no"))
            title = self._text(li.select_one(".tracklist__title")) or self._text(
                li.select_one(".tracklist__main")
            )
            if not title:
                continue
            tracks.append(f"{no}. {title}" if no else title)
        if tracks:
            meta["extra"]["tracks"] = tracks

        # Pokedora store cross-link — CAnDY BIBInBA sells through Pocket Drama CD.
        for a in soup.select("a[href*='pokedora.com']"):
            href = a.get("href", "")
            if "product_id=" in href:
                meta["extra"]["pokedora_url"] = href
                break

        # Store-exclusive bonus drama blurb (ポケドラ限定特典…).
        bonus = " ".join(
            self._text(e)
            for e in soup.select(".product__special .the_content")
            if self._text(e)
        ).strip()
        if bonus:
            meta["extra"]["bonus"] = bonus

        return meta

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _text(el) -> str:
        return el.get_text(" ", strip=True) if el else ""

    @staticmethod
    def _split_names(value: str) -> list[str]:
        out = []
        for part in _NAME_SEP_RE.split(value or ""):
            part = part.strip()
            if part and part.casefold() not in _FILLER_NAMES:
                out.append(part)
        return out
