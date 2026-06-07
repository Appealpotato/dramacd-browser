"""Full DLsite work-page HTML parser.

scraper.py normally gets title/circle/cover/tags/release date from the
JSON product API and only scrapes the work page for enrichment (seiyuu,
series, description). For a **delisted** work the API is gone and all we
have is an archived copy of the work page — this parser extracts the
complete scraper-shaped metadata dict from that HTML alone, so the result
flows through database.update_item_metadata unchanged.

Pure and network-free; selectors verified against live markup 2026-06.
Every selector is guarded — archived HTML can be partial."""
import re

from bs4 import BeautifulSoup

from .base import normalize_date


def parse_dlsite_work_html(html: str, *, source_url: str | None = None) -> dict:
    """Parse a DLsite work page into scraper-shaped metadata keys
    (title, circle, cover_url, tags, release_date, seiyuu, series,
    description, age_rating). Missing fields are simply absent."""
    soup = BeautifulSoup(html, "html.parser")
    result: dict = {}

    h1 = soup.find("h1", id="work_name")
    if h1:
        title = h1.get_text(strip=True)
        if title:
            result["title"] = title

    maker = soup.select_one(".maker_name a") or soup.select_one(".maker_name")
    if maker:
        circle = maker.get_text(strip=True)
        if circle:
            result["circle"] = circle

    og = soup.select_one('meta[property="og:image"][content]')
    if og and og["content"]:
        cover = og["content"]
        if cover.startswith("//"):
            cover = "https:" + cover
        result["cover_url"] = cover

    # Outline table: <th>label</th><td>value</td> rows. Older markup eras
    # (Wayback snapshots) suffix labels with " : " — normalize before match.
    outline = soup.find(id="work_outline") or soup
    for th in outline.find_all("th"):
        td = th.find_next_sibling("td")
        if not td:
            continue
        label = re.sub(r"[\s:：]+$", "", th.get_text(strip=True))
        if label == "販売日":
            date = normalize_date(td.get_text(" ", strip=True))
            if date:
                result["release_date"] = date
        elif label in ("シリーズ名", "Series"):
            a = td.find("a")
            series = (a or td).get_text(strip=True)
            if series:
                result["series"] = series
        elif label in ("声優", "Voice Actor"):
            links = td.find_all("a")
            seiyuu = ([a.get_text(strip=True) for a in links] if links
                      else [td.get_text(strip=True)])
            seiyuu = [s for s in seiyuu if s]
            if seiyuu:
                result["seiyuu"] = seiyuu
        elif label in ("年齢指定", "Age Ratings"):
            rating = td.get_text(strip=True)
            if rating:
                result["age_rating"] = rating
        elif label in ("ジャンル", "Genre"):
            tags = [a.get_text(strip=True) for a in td.find_all("a")]
            tags = [t for t in tags if t]
            if tags:
                result["tags"] = tags

    # Same selectors fetch_product_page uses for the rich description
    intro_div = (soup.find("div", class_="work_parts_area")
                 or soup.find("div", {"itemprop": "description"}))
    if intro_div:
        desc = intro_div.get_text(separator="\n", strip=True)[:5000]
        if desc:
            result["description"] = desc

    # Fallback age rating from the genre icon block (same as scraper.py)
    if "age_rating" not in result:
        age_div = soup.find("div", class_="work_genre")
        if age_div:
            rating_span = age_div.find("span", class_="icon_rating")
            if rating_span and rating_span.get_text(strip=True):
                result["age_rating"] = rating_span.get_text(strip=True)

    if source_url:
        result["source_page"] = source_url

    return result


def looks_like_work_page(html: str) -> bool:
    """Cheap sanity check that archived HTML is actually a work page and
    not an error/redirect interstitial the crawler happened to save."""
    return bool(html) and ('id="work_name"' in html or "work_outline" in html)


_RJ_IN_URL_RE = re.compile(r"product_id/((?:RJ|BJ|VJ)\d+)", re.I)


def code_from_work_url(url: str) -> str | None:
    m = _RJ_IN_URL_RE.search(url or "")
    return m.group(1).upper() if m else None
