"""Pluggable metadata sources for custom entries (manual drama CDs and
tokutens). Each source wraps one external site: a URL-pattern matcher, a
product-page parser, and (optionally) a search. All return the same
normalized dict shape so the API/UI layer is source-agnostic.

DLsite is NOT here — scraper.py owns the DLsite flow for scanned items.
These sources back the paste-URL / search flow for entries that have no
DLsite page (store tokutens, physical-only releases, etc.)."""
from .animate import AnimateSource
from .base import MetadataSource, SourceError
from .booth import BoothSource
from .chilchil import ChilChilSource
from .digiket import DigiketSource
from .dlsite import DLsiteSource
from .fanza import FanzaSource
from .gamers import GamersSource
from .gyutto import GyuttoSource
from .melon import MelonbooksSource
from .rejet import RejetSource
from .stellaworth import StellaworthSource

# Order matters only for documentation; URL dispatch is exact per-source.
# (Toranoana is absent by necessity: both the live EC site and every Wayback
# snapshot of it serve bot-blocked "アクセスエラー" pages, so its markup could
# not be captured for a parser — revisit if access opens up.)
SOURCES: list[MetadataSource] = [
    DLsiteSource(),
    BoothSource(),
    FanzaSource(),
    MelonbooksSource(),
    DigiketSource(),
    GyuttoSource(),
    GamersSource(),
    AnimateSource(),
    StellaworthSource(),
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
