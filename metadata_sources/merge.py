"""Merge N normalized metadata dicts (volumes of one series) into one.

Used by the multi-volume flow: the user multi-selects search hits for an
archive that holds e.g. Vol.1-8 of a series, each volume is fetched, and
this collapses them into a single payload for the one library entry.
Per-volume detail survives in extra["volumes"] and in the multi-line
provenance note the apply layer builds from it."""
import re

from .base import empty_metadata

# Trailing per-volume decorations stripped from the common title prefix:
# "Vol.8", "第3巻", "(1)", "上/下", connective punctuation, CV credits.
_TITLE_TAIL_RE = re.compile(
    r"[\s　]*(?:"
    r"Vol\.?\s*\d*|VOL\.?\s*\d*|vol\.?\s*\d*"
    r"|第\s*\d*\s*[巻話章]?"
    r"|[（(]\s*\d*\s*[)）]?"
    r"|[0-9０-９]+"
    r"|[‐－―\-~〜・:：「『]"
    r")+$"
)


def _common_title(titles: list[str]) -> str | None:
    """Longest common prefix of the volume titles, de-decorated. Falls back
    to the first title when the volumes share almost nothing (< 4 chars)."""
    titles = [t for t in (titles or []) if t]
    if not titles:
        return None
    if len(titles) == 1:
        return titles[0]
    prefix = titles[0]
    for t in titles[1:]:
        while prefix and not t.startswith(prefix):
            prefix = prefix[:-1]
    cleaned = _TITLE_TAIL_RE.sub("", prefix).strip()
    if len(cleaned) >= 4:
        return cleaned
    return titles[0]


def _first(metas: list[dict], key: str):
    for m in metas:
        if m.get(key):
            return m[key]
    return None


def _all_equal(values: list) -> bool:
    present = [v for v in values if v]
    return len(present) > 1 and all(v == present[0] for v in present)


def _volume_summary(meta: dict) -> dict:
    """The per-volume slice kept under extra['volumes'] (and used by the
    apply layer's note stamp)."""
    return {
        "title": meta.get("title"),
        "source": meta.get("source"),
        "source_url": meta.get("source_url"),
        "catalog_number": meta.get("catalog_number"),
        "jan": meta.get("jan"),
        "release_date": meta.get("release_date"),
        "price": meta.get("price"),
        "cover_url": meta.get("cover_url"),
    }


def merge_metadata(metas: list[dict]) -> dict:
    """Collapse per-volume metadata dicts into one normalized payload.

    Rules: title = common prefix; cast = ordered union; release_date =
    earliest; description deduped (series blurbs are often identical) or
    joined under per-volume headers; cover = first volume's; per-volume
    identifiers (catalog/jan/price) stay out of the merged scalars unless
    they're identical across all volumes — the full set always lives in
    extra['volumes']."""
    metas = [m for m in (metas or []) if m]
    if not metas:
        raise ValueError("merge_metadata: nothing to merge")
    if len(metas) == 1:
        return metas[0]

    merged = empty_metadata(metas[0].get("source") or "?", metas[0].get("source_url") or "")
    merged["title"] = _common_title([m.get("title") for m in metas])
    merged["maker"] = _first(metas, "maker")
    merged["series"] = _first(metas, "series")
    merged["cover_url"] = _first(metas, "cover_url")

    dates = sorted(d for d in (m.get("release_date") for m in metas) if d)
    merged["release_date"] = dates[0] if dates else None

    for m in metas:
        for name in m.get("seiyuu") or []:
            if name and name not in merged["seiyuu"]:
                merged["seiyuu"].append(name)

    # Description: identical blurbs collapse to one; differing blurbs are
    # joined under their volume titles.
    descs = []
    for m in metas:
        d = (m.get("description") or "").strip()
        if d and d not in descs:
            descs.append(d)
    if len(descs) == 1:
        merged["description"] = descs[0]
    elif descs:
        parts = []
        for m in metas:
            d = (m.get("description") or "").strip()
            if d:
                parts.append(f"■ {m.get('title') or '?'}\n{d}")
        merged["description"] = "\n\n".join(parts)

    # Identical-across-volumes scalars survive; differing ones defer to
    # extra['volumes'].
    for key in ("price", "jan", "catalog_number"):
        values = [m.get(key) for m in metas]
        if _all_equal(values):
            merged[key] = _first(metas, key)

    merged["extra"]["volumes"] = [_volume_summary(m) for m in metas]
    return merged
