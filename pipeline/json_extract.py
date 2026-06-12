"""Shared robust JSON extraction for LLM responses.

Every provider integration needs the same defenses, learned the hard way on
real data:
  • reasoning models (DeepSeek-R1 family) prepend <think>…</think> blocks
  • chatty models wrap the JSON in markdown code fences or prose
  • translated text containing quoted speech/nicknames breaks string escaping
    (e.g. 「うーちゃん」 rendered as "Uu-chan" inside a JSON string — observed
    from claude-sonnet on RJ01432516)

`loads_robust` runs the full chain and raises ValueError with a snippet of
the unparseable content, so the surfaced error says WHY. JSONDecodeError is
a ValueError subclass, so callers that caught the old json.loads errors via
ValueError/Exception keep working.
"""
import json
import re

_THINK_RE = re.compile(r"(?s)<think>.*?</think>")


def strip_reasoning(raw: str) -> str:
    """Drop in-band chain-of-thought; it is never part of the answer."""
    return _THINK_RE.sub("", str(raw or "")).strip()


def strip_code_fence(payload: str) -> str:
    payload = (payload or "").strip()
    if payload.startswith("```"):
        payload = payload.strip("`")
        marker_idx = payload.find("\n")
        if marker_idx >= 0:
            payload = payload[marker_idx + 1:]
    payload = payload.strip()
    if payload.startswith("json"):
        payload = payload[4:].strip()
    if payload.endswith("```"):
        payload = payload[:-3].strip()
    return payload.strip()


def escape_inner_quotes(payload: str) -> str:
    """Best-effort repair for the single most common LLM JSON defect:
    literal unescaped double quotes inside string values. Walks the payload
    tracking in-string state; a quote inside a string only ENDS it if the
    next non-space character is a JSON delimiter, otherwise it's content and
    gets escaped. Heuristic — quoted speech immediately followed by a comma
    still parses as a string end — but it converts the common failure into a
    clean parse and never alters already-valid JSON string boundaries."""
    out = []
    in_str = False
    i = 0
    n = len(payload)
    while i < n:
        c = payload[i]
        if not in_str:
            if c == '"':
                in_str = True
            out.append(c)
        elif c == "\\" and i + 1 < n:
            out.append(c)
            out.append(payload[i + 1])
            i += 2
            continue
        elif c == '"':
            j = i + 1
            while j < n and payload[j] in " \t\r\n":
                j += 1
            if j >= n or payload[j] in ",:}]":
                in_str = False
                out.append(c)
            else:
                out.append('\\"')
        else:
            out.append(c)
        i += 1
    return "".join(out)


def loads_robust(raw: str):
    """Parse a model response that SHOULD be pure JSON but often isn't.
    Chain: strip reasoning → strip fences → strict parse → quote-repair →
    outermost {...}/[...] span (each span also quote-repaired)."""
    payload = strip_code_fence(strip_reasoning(raw))
    try:
        return json.loads(payload)
    except Exception:
        pass
    try:
        return json.loads(escape_inner_quotes(payload))
    except Exception:
        pass
    # Prose around the JSON: take the outermost object/array span. Try the
    # bracket type that OPENS FIRST before the other, so an array containing
    # objects isn't mistaken for its first inner object.
    spans = []
    for opener, closer in (("{", "}"), ("[", "]")):
        first = payload.find(opener)
        last = payload.rfind(closer)
        if first >= 0 and last > first:
            spans.append((first, payload[first:last + 1]))
    for _, candidate in sorted(spans):
        for attempt in (candidate, escape_inner_quotes(candidate)):
            try:
                return json.loads(attempt)
            except Exception:
                continue
    raise ValueError(f"no parseable JSON in model response: {payload[:200]!r}")
