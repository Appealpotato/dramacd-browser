"""Pluggable metadata sources for custom entries (manual drama CDs and
tokutens). Each source wraps one external site: a URL-pattern matcher, a
product-page parser, and (optionally) a search. All return the same
normalized dict shape so the API/UI layer is source-agnostic.

DLsite is NOT here — scraper.py owns the DLsite flow for scanned items.
These sources back the paste-URL / search flow for entries that have no
DLsite page (store tokutens, physical-only releases, etc.)."""
from .base import MetadataSource, SourceError
from .chilchil import ChilChilSource
from .dlsite import DLsiteSource
from .gamers import GamersSource
from .rejet import RejetSource

# Order matters only for documentation; URL dispatch is exact per-source.
SOURCES: list[MetadataSource] = [
    DLsiteSource(),
    GamersSource(),
    ChilChilSource(),
    RejetSource(),
]

_BY_NAME = {s.name: s for s in SOURCES}


def match_url(url: str) -> MetadataSource | None:
    """Return the source whose URL pattern matches, or None."""
    for source in SOURCES:
        if source.matches_url(url):
            return source
    return None


def get_source(name: str) -> MetadataSource | None:
    return _BY_NAME.get(name)


def list_sources() -> list[dict]:
    """Source descriptors for the UI (search dropdown, labels)."""
    return [
        {
            "name": s.name,
            "label": s.label,
            "supports_search": s.supports_search,
            "url_example": s.url_example,
        }
        for s in SOURCES
    ]
