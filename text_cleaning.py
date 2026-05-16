import re

TIMESTAMP_LINE_RE = re.compile(
    r"^\s*\d{1,2}:\d{2}:\d{2}(?:[.,]\d{1,3})?\s*-->\s*\d{1,2}:\d{2}:\d{2}(?:[.,]\d{1,3})?(?:\s+.*)?$"
)
INLINE_TIMESTAMP_RE = re.compile(r"<\d{2}:\d{2}:\d{2}(?:[.,]\d{1,3})?>")
HTML_TAG_RE = re.compile(r"</?[^>]+>")
SPEAKER_PREFIX_RE = re.compile(
    r"^\s*[-\u2013\u2014]?\s*(?:[A-Za-z][A-Za-z0-9_ .-]{0,30}|[\u3040-\u30ff\u3400-\u9fff]{1,20})\s*[:\uff1a]\s*"
)
BRACKETED_SFX_RE = re.compile(r"^\s*[\[(\uff08\u3010].*?[\])\uff09\u3011]\s*$")
PURE_NUMBER_RE = re.compile(r"^\s*[0-9\uff10-\uff19]+\s*$")


def is_timestamp_line(line: str) -> bool:
    return "-->" in line and bool(TIMESTAMP_LINE_RE.match(line))


def normalize_subtitle_line(line: str) -> str:
    cleaned = line.strip()
    cleaned = INLINE_TIMESTAMP_RE.sub("", cleaned)
    cleaned = HTML_TAG_RE.sub("", cleaned)
    cleaned = cleaned.replace("&nbsp;", " ").replace("&amp;", "&")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = SPEAKER_PREFIX_RE.sub("", cleaned)
    return cleaned.strip()


def should_skip_subtitle_line(line: str, drop_sfx: bool = True) -> bool:
    if not line:
        return True
    if is_timestamp_line(line):
        return True
    if PURE_NUMBER_RE.match(line):
        return True
    if drop_sfx and BRACKETED_SFX_RE.match(line):
        return True
    return False


def clean_dialogue_line(line: str, drop_sfx: bool = True) -> str:
    if should_skip_subtitle_line(line, drop_sfx=drop_sfx):
        return ""
    normalized = normalize_subtitle_line(line)
    if should_skip_subtitle_line(normalized, drop_sfx=drop_sfx):
        return ""
    return normalized


def build_clean_translation_source(segments: list[dict], drop_sfx: bool = True) -> dict:
    cleaned_segments = []
    cleaned_lines = []

    for seg in segments:
        raw_text = str(seg.get("text") or "")
        cleaned = clean_dialogue_line(raw_text, drop_sfx=drop_sfx)
        cleaned_segments.append(
            {
                "segment_index": seg.get("segment_index"),
                "text": raw_text,
                "clean_text": cleaned,
                "start_seconds": seg.get("start_seconds"),
                "end_seconds": seg.get("end_seconds"),
            }
        )
        if cleaned:
            cleaned_lines.append(cleaned)

    return {
        "segments": cleaned_segments,
        "line_count": len(cleaned_lines),
        "text": "\n".join(cleaned_lines),
    }
