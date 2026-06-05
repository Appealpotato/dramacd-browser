"""Shared base for external metadata sources (Gamers, Chil-Chil, ...).

A source = URL matcher + product-page parser + optional search. Parsing is
kept in pure `parse_*` methods that take HTML strings so tests can run
against fixture files without network access."""
import logging
import re

import httpx

logger = logging.getLogger(__name__)

# Plain-browser headers. These sites are server-rendered and httpx-friendly
# (verified 2026-06); no stealth machinery needed.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

REQUEST_TIMEOUT = 30.0


class SourceError(Exception):
    """Fetch/parse failure with a user-facing message."""


def empty_metadata(source: str, source_url: str) -> dict:
    """The normalized shape every fetch_by_url returns. `seiyuu` is a list of
    names; site-specific extras (staff, tags, tokuten names, cast detail)
    live under `extra` so the apply layer can stamp them into notes."""
    return {
        "source": source,
        "source_url": source_url,
        "title": None,
        "title_en": None,
        "release_date": None,   # YYYY-MM-DD
        "seiyuu": [],
        "description": None,
        "cover_url": None,
        "price": None,          # display string, e.g. "3,630円"
        "jan": None,
        "catalog_number": None, # 品番
        "maker": None,
        "series": None,
        "extra": {},
    }


def normalize_date(text: str | None) -> str | None:
    """Pull the first YYYY/MM/DD-ish date out of text -> YYYY-MM-DD."""
    if not text:
        return None
    m = re.search(r"(\d{4})[/\-年.](\d{1,2})[/\-月.](\d{1,2})", text)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return f"{y:04d}-{mo:02d}-{d:02d}"


class MetadataSource:
    name = "base"             # registry key; matches a tokutens.shop value
    label = "Base"
    url_example = ""
    supports_search = False
    _url_re: re.Pattern | None = None

    def matches_url(self, url: str) -> bool:
        return bool(self._url_re and self._url_re.search(url or ""))

    async def _get(self, client: httpx.AsyncClient, url: str, **kwargs) -> str:
        try:
            resp = await client.get(
                url, headers=HEADERS, follow_redirects=True,
                timeout=REQUEST_TIMEOUT, **kwargs,
            )
        except httpx.HTTPError as exc:
            raise SourceError(f"{self.label}: request failed ({exc})") from exc
        if resp.status_code != 200:
            raise SourceError(f"{self.label}: HTTP {resp.status_code} for {url}")
        return resp.text

    async def _post(self, client: httpx.AsyncClient, url: str, data: dict) -> str:
        try:
            resp = await client.post(
                url, headers=HEADERS, data=data, follow_redirects=True,
                timeout=REQUEST_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise SourceError(f"{self.label}: request failed ({exc})") from exc
        if resp.status_code != 200:
            raise SourceError(f"{self.label}: HTTP {resp.status_code} for {url}")
        return resp.text

    async def fetch_by_url(self, client: httpx.AsyncClient, url: str) -> dict:
        raise NotImplementedError

    async def search(self, client: httpx.AsyncClient, query: str) -> list[dict]:
        raise NotImplementedError(f"{self.label} does not support search")
