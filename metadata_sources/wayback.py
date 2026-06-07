"""Wayback Machine fallback for delisted DLsite works.

DLsite permanently delists doujin works all the time; the JSON API then
404s forever. archive.org usually still has the work page — this module
finds the closest snapshot, pulls the **raw** archived HTML (the `id_`
timestamp suffix strips the Wayback toolbar/rewriting), and parses it with
metadata_sources.dlsite_html into a scraper-shaped dict.

Covers ride through the archive too: `wayback_image_url` rewrites a dead
img.dlsite.jp URL onto the snapshot's timestamp with the `im_` suffix so
scraper.download_cover can fetch the archived bytes."""
import asyncio
import logging

import httpx

from config import DLSITE_SITE_SECTIONS, WAYBACK_DELAY
from .dlsite_html import looks_like_work_page, parse_dlsite_work_html

logger = logging.getLogger(__name__)

AVAILABILITY_API = "https://archive.org/wayback/available"
HEADERS = {"User-Agent": "dramacd-browser (Wayback fallback for delisted works)"}
REQUEST_TIMEOUT = 30.0


def work_page_url(code: str, section: str) -> str:
    return f"https://www.dlsite.com/{section}/work/=/product_id/{code}.html"


def snapshot_raw_url(timestamp: str, original_url: str) -> str:
    """Raw archived HTML — no Wayback toolbar markup injected."""
    return f"https://web.archive.org/web/{timestamp}id_/{original_url}"


def wayback_image_url(timestamp: str, image_url: str) -> str:
    """Archived rendition of an image URL (im_ suffix serves the bytes)."""
    if image_url.startswith("//"):
        image_url = "https:" + image_url
    return f"https://web.archive.org/web/{timestamp}im_/{image_url}"


async def find_snapshot(client: httpx.AsyncClient, url: str) -> dict | None:
    """Closest archived 200 snapshot via the availability API.
    Returns {"url", "timestamp"} or None."""
    try:
        resp = await client.get(
            AVAILABILITY_API, params={"url": url},
            headers=HEADERS, timeout=REQUEST_TIMEOUT, follow_redirects=True,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    snap = (data.get("archived_snapshots") or {}).get("closest") or {}
    if not snap.get("available") or str(snap.get("status")) != "200":
        return None
    if not snap.get("url") or not snap.get("timestamp"):
        return None
    return {"url": snap["url"], "timestamp": snap["timestamp"]}


async def fetch_dlsite_via_wayback(client: httpx.AsyncClient, code: str) -> dict | None:
    """Try archived work pages for `code` across DLsite sections; first
    parsable snapshot wins. Returns scraper-shaped metadata (with
    raw["_wayback"] provenance and an archive-routed cover_url) or None."""
    for section in DLSITE_SITE_SECTIONS:
        original_url = work_page_url(code, section)
        snap = await find_snapshot(client, original_url)
        await asyncio.sleep(WAYBACK_DELAY)
        if not snap:
            continue

        raw_url = snapshot_raw_url(snap["timestamp"], original_url)
        try:
            resp = await client.get(raw_url, headers=HEADERS,
                                    timeout=REQUEST_TIMEOUT, follow_redirects=True)
        except httpx.HTTPError as exc:
            logger.debug(f"[wayback] fetch failed for {raw_url}: {exc}")
            continue
        await asyncio.sleep(WAYBACK_DELAY)
        if resp.status_code != 200 or not looks_like_work_page(resp.text):
            continue

        metadata = parse_dlsite_work_html(resp.text, source_url=original_url)
        if not metadata.get("title"):
            continue  # not enough to be useful

        # Dead img.dlsite.jp covers download through the archive instead
        if metadata.get("cover_url"):
            metadata["cover_url"] = wayback_image_url(snap["timestamp"],
                                                      metadata["cover_url"])

        # Provenance persists for free inside metadata_raw
        metadata["raw"] = {
            "_wayback": {
                "snapshot_url": raw_url,
                "timestamp": snap["timestamp"],
                "original_url": original_url,
            }
        }
        logger.info(f"[wayback] rescued {code} from {raw_url}")
        return metadata

    return None
