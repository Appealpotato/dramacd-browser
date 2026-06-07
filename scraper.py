import asyncio
import json
import logging
import inspect
from datetime import datetime
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

import config
from config import COVERS_DIR, DLSITE_REQUEST_DELAY, DLSITE_SITE_SECTIONS, DLSITE_PROXY_URL
from metadata_sources.wayback import fetch_dlsite_via_wayback

logger = logging.getLogger(__name__)

# Track scraping progress for the UI
scrape_progress = {
    "job_id": None,
    "running": False,
    "paused": False,
    "stopping": False,
    "stopped": False,
    "total": 0,
    "completed": 0,
    "current": None,
    "errors": [],
    "success": 0,
    "failed": 0,
    "skipped": 0,
    "error_summary": {},
    "started_at": None,
    "finished_at": None,
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
}

MAX_RETRIES = 3
BASE_RETRY_DELAY = 0.8
REQUEST_TIMEOUT = 20.0


def _reason_from_status(status_code: int) -> str:
    if status_code == 404:
        return "not_found"
    if status_code == 429:
        return "rate_limited"
    if 500 <= status_code <= 599:
        return "server_error"
    if status_code in (401, 403):
        return "access_denied"
    return f"http_{status_code}"


def _record_error(code: str, reason: str, message: str):
    scrape_progress["errors"].append({"code": code, "reason": reason, "error": message})
    scrape_progress["error_summary"][reason] = scrape_progress["error_summary"].get(reason, 0) + 1


def _pick_final_reason(reasons: list[str]) -> str:
    if not reasons:
        return "unknown_error"

    priority = [
        "rate_limited",
        "timeout",
        "network_error",
        "server_error",
        "access_denied",
        "parse_error",
        "not_found",
    ]
    reason_set = set(reasons)
    for reason in priority:
        if reason in reason_set:
            return reason
    return reasons[-1]


async def _request_with_retry(client: httpx.AsyncClient, url: str) -> tuple[httpx.Response | None, str | None]:
    last_reason = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = await client.get(url, headers=HEADERS, follow_redirects=True)

            if resp.status_code == 200:
                # Force UTF-8 encoding to prevent mojibake with Japanese text
                resp.encoding = 'utf-8'
                return resp, None

            reason = _reason_from_status(resp.status_code)
            last_reason = reason

            retryable = reason in {"rate_limited", "server_error"}
            if retryable and attempt < MAX_RETRIES:
                delay = BASE_RETRY_DELAY * (2 ** attempt)
                await asyncio.sleep(delay)
                continue

            return None, reason

        except httpx.TimeoutException:
            last_reason = "timeout"
            if attempt < MAX_RETRIES:
                delay = BASE_RETRY_DELAY * (2 ** attempt)
                await asyncio.sleep(delay)
                continue
            return None, last_reason
        except httpx.RequestError:
            last_reason = "network_error"
            if attempt < MAX_RETRIES:
                delay = BASE_RETRY_DELAY * (2 ** attempt)
                await asyncio.sleep(delay)
                continue
            return None, last_reason
        except Exception:
            return None, "unknown_error"

    return None, last_reason or "unknown_error"


async def fetch_product_json(client: httpx.AsyncClient, product_code: str, site: str, locale: str = "ja-JP") -> tuple[dict | None, str | None]:
    """Fetch product JSON from DLsite API."""
    url = f"https://www.dlsite.com/{site}/api/=/product.json?workno={product_code}&locale={locale}"
    resp, reason = await _request_with_retry(client, url)
    if not resp:
        return None, reason

    try:
        data = resp.json()
    except json.JSONDecodeError:
        return None, "parse_error"

    if isinstance(data, list) and len(data) > 0:
        return data[0], None
    if isinstance(data, dict) and data:
        return data, None
    return None, "not_found"


async def fetch_product_page(client: httpx.AsyncClient, product_code: str, site: str, locale: str = "ja-JP") -> tuple[dict | None, str | None]:
    """Scrape product page HTML for additional metadata (seiyuu, description, etc.)."""
    url = f"https://www.dlsite.com/{site}/work/=/product_id/{product_code}.html?locale={locale}"
    resp, reason = await _request_with_retry(client, url)
    if not resp:
        return None, reason

    try:
        soup = BeautifulSoup(resp.text, "html.parser")
        result = {}

        # Voice actors (seiyuu) - JP/EN headers
        seiyuu_row = soup.find("th", string=lambda t: t and ("\u58f0\u512a" in t or "Voice" in t))
        if seiyuu_row:
            td = seiyuu_row.find_next_sibling("td")
            if td:
                links = td.find_all("a")
                result["seiyuu"] = [a.get_text(strip=True) for a in links] if links else [td.get_text(strip=True)]

        # Series - JP/EN headers
        series_row = soup.find("th", string=lambda t: t and ("\u30b7\u30ea\u30fc\u30ba\u540d" in t or "Series" in t))
        if series_row:
            td = series_row.find_next_sibling("td")
            if td:
                a = td.find("a")
                result["series"] = a.get_text(strip=True) if a else td.get_text(strip=True)

        # Description / introduction
        intro_div = soup.find("div", class_="work_parts_area") or soup.find("div", {"itemprop": "description"})
        if intro_div:
            result["description"] = intro_div.get_text(separator="\n", strip=True)[:5000]

        # Age rating
        age_div = soup.find("div", class_="work_genre")
        if age_div:
            rating_span = age_div.find("span", class_="icon_rating")
            if rating_span:
                result["age_rating"] = rating_span.get_text(strip=True)

        return result, None
    except Exception:
        return None, "parse_error"


async def download_cover(client: httpx.AsyncClient, cover_url: str, product_code: str) -> tuple[str | None, str | None]:
    """Download cover art and save locally."""
    if not cover_url:
        return None, None

    COVERS_DIR.mkdir(parents=True, exist_ok=True)

    # Determine extension from URL
    ext = ".jpg"
    if ".png" in cover_url.lower():
        ext = ".png"
    elif ".webp" in cover_url.lower():
        ext = ".webp"

    local_path = COVERS_DIR / f"{product_code}{ext}"

    if cover_url.startswith("//"):
        cover_url = "https:" + cover_url

    resp, reason = await _request_with_retry(client, cover_url)
    if not resp:
        return None, reason

    try:
        local_path.write_bytes(resp.content)
        return str(local_path.relative_to(COVERS_DIR.parent.parent)), None
    except Exception:
        return None, "io_error"


def parse_api_response(data: dict) -> dict:
    """Parse DLsite API JSON response into our metadata format."""
    result = {}

    result["title"] = data.get("work_name") or data.get("name", {}).get("ja-JP")
    result["circle"] = data.get("maker_name") or data.get("maker", {}).get("name", {}).get("ja-JP")

    # Cover image
    img = data.get("work_image") or data.get("image_main", {}).get("url")
    if img:
        if img.startswith("//"):
            img = "https:" + img
        result["cover_url"] = img

    # Genre tags
    genres = data.get("genres") or data.get("genre", [])
    if isinstance(genres, list):
        result["tags"] = [
            g.get("name") or g.get("text", "")
            for g in genres
            if isinstance(g, dict) and (g.get("name") or g.get("text"))
        ]
    elif isinstance(genres, dict):
        result["tags"] = list(genres.values())
    else:
        result["tags"] = []

    # Release date
    result["release_date"] = data.get("regist_date") or data.get("work_date") or data.get("sales_date")

    # Description
    result["description"] = data.get("intro_s") or data.get("description", "")

    # Age rating
    age = data.get("age_category_string") or data.get("age_category")
    if isinstance(age, int):
        age_map = {1: "ALL", 2: "R15", 3: "R18"}
        result["age_rating"] = age_map.get(age, str(age))
    else:
        result["age_rating"] = str(age) if age else None

    return result


async def fetch_metadata_for_code(client: httpx.AsyncClient, product_code: str, wayback: bool = True) -> tuple[dict | None, str | None]:
    """Fetch complete metadata for a single product code.

    Tries multiple DLsite site sections until a match is found. When every
    lookup hard-404s (delisted work) and `wayback` is on, falls back to an
    archived copy of the work page via the Wayback Machine.
    Returns (metadata, error_reason).
    """
    metadata = None
    matched_site = None
    matched_code = product_code
    failure_reasons: list[str] = []

    # Try JSON API across different site sections
    for site in DLSITE_SITE_SECTIONS:
        data, reason = await fetch_product_json(client, product_code, site, locale="ja-JP")
        if data:
            metadata = parse_api_response(data)
            metadata["raw"] = data
            matched_site = site
            logger.info(f"Found {product_code} on {site}")
            break
        if reason:
            failure_reasons.append(reason)
        await asyncio.sleep(0.3)

    if not metadata:
        # If RJ didn't work, maybe try as BJ or VJ
        prefix = product_code[:2]
        number = product_code[2:]
        alt_prefixes = {"RJ": ["BJ", "VJ"], "BJ": ["RJ", "VJ"], "VJ": ["RJ", "BJ"]}
        for alt in alt_prefixes.get(prefix, []):
            alt_code = f"{alt}{number}"
            for site in DLSITE_SITE_SECTIONS:
                data, reason = await fetch_product_json(client, alt_code, site, locale="ja-JP")
                if data:
                    metadata = parse_api_response(data)
                    metadata["raw"] = data
                    matched_site = site
                    matched_code = alt_code
                    metadata["actual_code"] = alt_code
                    logger.info(f"Found {product_code} as {alt_code} on {site}")
                    break
                if reason:
                    failure_reasons.append(reason)
                await asyncio.sleep(0.3)
            if metadata:
                break

    if not metadata:
        final_reason = _pick_final_reason(failure_reasons)
        # Wayback rescue fires ONLY on a hard 404 everywhere (delisted work).
        # Transient reasons (rate_limited/timeout/server_error/...) must
        # surface unchanged so retries stay meaningful.
        if wayback and final_reason == "not_found":
            wb_meta = await fetch_dlsite_via_wayback(client, product_code)
            if wb_meta:
                # No EN locale pass for archived pages — mirror JP values so
                # downstream EN fields are populated.
                wb_meta.setdefault("title_en", wb_meta.get("title"))
                wb_meta.setdefault("tags_en", wb_meta.get("tags", []))
                wb_meta.setdefault("seiyuu_en", wb_meta.get("seiyuu", []))
                if wb_meta.get("cover_url"):
                    local_cover, cover_reason = await download_cover(client, wb_meta["cover_url"], product_code)
                    if local_cover:
                        wb_meta["cover_local"] = local_cover
                    elif cover_reason:
                        logger.debug(f"Wayback cover download skipped for {product_code}: {cover_reason}")
                return wb_meta, None
        return None, final_reason

    # Fetch English metadata for translated title/tags
    if matched_site:
        en_data, _ = await fetch_product_json(client, matched_code, matched_site, locale="en-US")
        if en_data:
            en_title = en_data.get("work_name") or en_data.get("name", {}).get("en-US")
            if en_title:
                metadata["title_en"] = en_title
            en_parsed = parse_api_response(en_data)
            metadata["tags_en"] = en_parsed.get("tags", [])
            if en_parsed.get("description"):
                metadata["description_en"] = en_parsed.get("description")
        await asyncio.sleep(0.3)

    # Scrape HTML page for additional data (seiyuu, series, richer description)
    if matched_site:
        page_data, page_reason = await fetch_product_page(client, matched_code, matched_site, locale="ja-JP")
        if page_data:
            if page_data.get("seiyuu"):
                metadata["seiyuu"] = page_data["seiyuu"]
            if page_data.get("series"):
                metadata["series"] = page_data["series"]
            if page_data.get("description") and len(page_data["description"]) > len(metadata.get("description", "")):
                metadata["description"] = page_data["description"]
            if page_data.get("age_rating") and not metadata.get("age_rating"):
                metadata["age_rating"] = page_data["age_rating"]
        elif page_reason:
            logger.debug(f"Page enrichment skipped for {product_code}: {page_reason}")

        en_page_data, _ = await fetch_product_page(client, matched_code, matched_site, locale="en-US")
        if en_page_data and en_page_data.get("seiyuu"):
            metadata["seiyuu_en"] = en_page_data["seiyuu"]
        if en_page_data and en_page_data.get("description"):
            if len(en_page_data["description"]) > len(metadata.get("description_en", "")):
                metadata["description_en"] = en_page_data["description"]
        await asyncio.sleep(0.3)

    metadata.setdefault("tags_en", metadata.get("tags", []))
    metadata.setdefault("seiyuu_en", metadata.get("seiyuu", []))
    metadata.setdefault("title_en", metadata.get("title"))  # Fallback to JP title if no EN translation

    # Download cover art
    if metadata.get("cover_url"):
        local_cover, cover_reason = await download_cover(client, metadata["cover_url"], matched_code)
        if local_cover:
            metadata["cover_local"] = local_cover
        elif cover_reason:
            logger.debug(f"Cover download skipped for {product_code}: {cover_reason}")

    return metadata, None


async def fetch_all_metadata(
    product_codes: list[str],
    force: bool = False,
    pause_event=None,
    stop_event=None,
    progress_callback=None,
):
    """Fetch metadata for a list of product codes with rate limiting.

    Updates scrape_progress dict for UI polling.
    """
    async def _emit_progress():
        if not progress_callback:
            return
        try:
            result = progress_callback(dict(scrape_progress))
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("Progress callback failed")

    scrape_progress.update({
        "running": True,
        "paused": False,
        "stopping": False,
        "stopped": False,
        "total": len(product_codes),
        "completed": 0,
        "current": None,
        "errors": [],
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "error_summary": {},
        "started_at": datetime.utcnow().isoformat() + "Z",
        "finished_at": None,
    })
    await _emit_progress()

    from database import get_db, update_item_metadata

    timeout = httpx.Timeout(REQUEST_TIMEOUT)
    client_kwargs = {"timeout": timeout}
    if DLSITE_PROXY_URL:
        logger.info(f"Using proxy for DLsite requests: {DLSITE_PROXY_URL}")
        client_kwargs["proxies"] = DLSITE_PROXY_URL
    async with httpx.AsyncClient(**client_kwargs) as client:
        for code in product_codes:
            if stop_event is not None and stop_event.is_set():
                scrape_progress["stopped"] = True
                await _emit_progress()
                break

            while pause_event is not None and pause_event.is_set():
                scrape_progress["paused"] = True
                if stop_event is not None and stop_event.is_set():
                    scrape_progress["paused"] = False
                    scrape_progress["stopped"] = True
                    await _emit_progress()
                    break
                await asyncio.sleep(0.25)
            if scrape_progress.get("stopped"):
                break

            scrape_progress["paused"] = False
            scrape_progress["stopping"] = bool(stop_event is not None and stop_event.is_set())
            scrape_progress["current"] = code

            if not force:
                # Check if we already have metadata
                db = await get_db()
                try:
                    cursor = await db.execute(
                        """SELECT title, title_en, tags_en FROM items WHERE product_code = ?""",
                        (code,),
                    )
                    row = await cursor.fetchone()
                    if row and row["title"] and row["title_en"] and row["tags_en"] not in (None, '', '[]'):
                        scrape_progress["completed"] += 1
                        scrape_progress["skipped"] += 1
                        await _emit_progress()
                        continue
                finally:
                    await db.close()

            try:
                metadata, reason = await fetch_metadata_for_code(client, code, wayback=config.WAYBACK_FALLBACK)
                if metadata:
                    await update_item_metadata(code, metadata)
                    scrape_progress["success"] += 1
                    logger.info(f"Saved metadata for {code}: {metadata.get('title', 'untitled')}")
                else:
                    final_reason = reason or "not_found"
                    _record_error(code, final_reason, "No metadata found on DLsite")
                    scrape_progress["failed"] += 1
                    logger.warning(f"No metadata found for {code} ({final_reason})")
            except Exception as e:
                _record_error(code, "unknown_error", str(e))
                scrape_progress["failed"] += 1
                logger.error(f"Error fetching metadata for {code}: {e}")

            scrape_progress["completed"] += 1
            await _emit_progress()
            await asyncio.sleep(DLSITE_REQUEST_DELAY)

    scrape_progress["running"] = False
    scrape_progress["paused"] = False
    scrape_progress["stopping"] = False
    scrape_progress["current"] = None
    scrape_progress["finished_at"] = datetime.utcnow().isoformat() + "Z"
    await _emit_progress()
    return scrape_progress
