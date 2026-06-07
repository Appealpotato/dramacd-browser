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
import re

import httpx

from config import DLSITE_SITE_SECTIONS, WAYBACK_DELAY
from .base import MetadataSource, SourceError, empty_metadata
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


class WaybackDLsiteSource(MetadataSource):
    """Manual paste source for archived DLsite work pages.

    Paste any web.archive.org snapshot URL of a DLsite work page and the
    exact snapshot the user chose is parsed (no availability lookup).
    Must be registered BEFORE DLsiteSource — its regex also matches the
    dlsite.com path embedded in archive URLs."""

    name = "wayback"
    label = "Wayback DLsite"
    url_example = ("https://web.archive.org/web/20160531032104/"
                   "https://www.dlsite.com/maniax/work/=/product_id/RJ001255.html")
    supports_search = False
    _url_re = re.compile(
        r"web\.archive\.org/web/(\d{4,14})(?:id_|im_|if_)?/(?:https?:/+)?(?:www\.)?"
        r"dlsite\.com/([a-z0-9\-]+)/work/=/product_id/((?:RJ|BJ|VJ)\d{6,8})",
        re.I,
    )

    async def fetch_by_url(self, client: httpx.AsyncClient, url: str) -> dict:
        m = self._url_re.search(url or "")
        if not m:
            raise SourceError(f"{self.label}: not an archived DLsite work URL: {url}")
        timestamp, section, code = m.group(1), m.group(2).lower(), m.group(3).upper()
        original_url = work_page_url(code, section)
        html = await self._get(client, snapshot_raw_url(timestamp, original_url))
        if not looks_like_work_page(html):
            raise SourceError(f"{self.label}: snapshot is not a work page (try another snapshot)")
        parsed = parse_dlsite_work_html(html, source_url=original_url)
        if not parsed.get("title"):
            raise SourceError(f"{self.label}: could not parse a title from the snapshot")
        return self._to_source_meta(parsed, url=url, timestamp=timestamp,
                                    original_url=original_url, code=code)

    def _to_source_meta(self, parsed: dict, *, url: str, timestamp: str,
                        original_url: str, code: str) -> dict:
        """Translate scraper-shaped parse output into the normalized source
        shape, with the full scraper payload riding in extra for the apply
        layer's adopt-code path."""
        meta = empty_metadata(self.name, url)
        meta["title"] = parsed.get("title")
        meta["seiyuu"] = list(parsed.get("seiyuu") or [])
        meta["description"] = parsed.get("description")
        meta["release_date"] = parsed.get("release_date")
        meta["maker"] = parsed.get("circle")
        meta["series"] = parsed.get("series")
        meta["catalog_number"] = code
        if parsed.get("cover_url"):
            meta["cover_url"] = wayback_image_url(timestamp, parsed["cover_url"])
        meta["extra"]["product_code"] = code
        meta["extra"]["tags"] = parsed.get("tags") or []
        meta["extra"]["age_rating"] = parsed.get("age_rating")
        meta["extra"]["wayback_snapshot"] = snapshot_raw_url(timestamp, original_url)

        # Scraper-shaped payload (same keys the auto fallback produces) so
        # adopt-code persists it without refetching.
        dl_meta = {k: v for k, v in parsed.items() if k != "source_page"}
        if meta["cover_url"]:
            dl_meta["cover_url"] = meta["cover_url"]
        dl_meta.setdefault("title_en", dl_meta.get("title"))
        dl_meta.setdefault("tags_en", dl_meta.get("tags", []))
        dl_meta.setdefault("seiyuu_en", dl_meta.get("seiyuu", []))
        dl_meta["raw"] = {"_wayback": {
            "snapshot_url": snapshot_raw_url(timestamp, original_url),
            "timestamp": timestamp,
            "original_url": original_url,
        }}
        meta["extra"]["dlsite_metadata"] = dl_meta
        return meta
