"""Native Anthropic Messages API translator.

For proxies / endpoints that speak Anthropic's native /messages format
(``x-api-key`` + ``anthropic-version`` headers, top-level ``system`` field,
``content`` array in the response). SillyTavern's "Claude" reverse-proxy
mode hits this same shape, which is why a URL that works there but not on
OpenAI Chat Completions belongs here instead.

We piggy-back on :class:`OpenRouterTrackTranslator` for all the JSON parsing /
chunk-coercion logic and only override the network call so the request body
and response shape match Anthropic's spec.
"""

from __future__ import annotations

import logging

import httpx

from config import llm_timeout
from pipeline.openrouter_translator import (
    CACHE_BREAKPOINT_MARKER,
    OpenRouterTrackTranslator,
)

logger = logging.getLogger(__name__)

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 4096
# Anthropic allows up to 4 cache_control breakpoints per request. We mark the
# most recent N user messages so each chunk extends the cached prefix.
MAX_CACHE_BREAKPOINTS = 4


def _supports_temperature(model: str) -> bool:
    """Newer Claude models (Fable 5, Opus 4.8 / 4.7) removed the sampling
    parameters — sending ``temperature`` returns a 400 ("temperature is
    deprecated for this model"). Detect those and omit it for them.

    Errs on the side of still sending temperature for anything we don't
    recognise, so non-Claude / proxied endpoints keep their existing behaviour."""
    m = (model or "").lower()
    if "fable" in m:
        return False
    # claude-opus-4-7 / 4-8 (and any later 4-9, 5-x) dropped sampling params too.
    for major, minor in (("4", 7), ("4", 8), ("4", 9)):
        if f"opus-{major}-{minor}" in m or f"opus{major}.{minor}" in m:
            return False
    return True


def anthropic_headers(api_key: str | None) -> dict:
    """Request headers for an Anthropic-format endpoint. Keyless local shims
    are valid — the x-api-key header is only sent when a key is configured
    (httpx rejects None header values outright)."""
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": ANTHROPIC_VERSION,
    }
    if api_key:
        headers["x-api-key"] = str(api_key)
    return headers


def _normalize_messages_url(base_url: str) -> str:
    """Resolve the user's base URL to ``{base}/messages``.

    Idempotent if the user already pasted a URL ending in ``/messages``;
    auto-strips a trailing ``/chat/completions`` since that path comes from
    OpenAI mode and would be wrong here."""
    raw = (base_url or "").strip()
    if not raw:
        return ""
    while raw.endswith("/"):
        raw = raw[:-1]
    if raw.endswith("/messages"):
        return raw
    if raw.endswith("/chat/completions"):
        raw = raw[: -len("/chat/completions")]
    return raw + "/messages"


class AnthropicCompatTrackTranslator(OpenRouterTrackTranslator):
    """Anthropic Messages API variant. Uses the same parsing/coercion as
    the OpenAI flavour but swaps the request shape and headers."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        provider_label: str = "openai_compat:anthropic",
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ):
        super().__init__(
            api_key=api_key,
            model=model,
            base_url=base_url,
            provider_label=provider_label,
        )
        # Override the OpenAI-style endpoint with the Anthropic one.
        self.endpoint = _normalize_messages_url(self.base_url)
        self.max_tokens = max_tokens

    def _build_anthropic_messages(self) -> list[dict]:
        """Convert chat history to Anthropic's content-block format, applying
        ``cache_control`` to the cacheable prefix of the most recent user
        messages (capped at MAX_CACHE_BREAKPOINTS to stay within Anthropic's
        per-request limit)."""
        # Pick the indices of user messages that carry our marker, newest first.
        eligible_indices = [
            i for i, m in enumerate(self._history)
            if m.get("role") == "user"
            and isinstance(m.get("content"), str)
            and CACHE_BREAKPOINT_MARKER in m["content"]
        ]
        cache_set = set(eligible_indices[-MAX_CACHE_BREAKPOINTS:])

        out: list[dict] = []
        for i, msg in enumerate(self._history):
            role = msg.get("role")
            content = msg.get("content")
            if role == "user" and isinstance(content, str) and CACHE_BREAKPOINT_MARKER in content:
                stable, variable = content.split(CACHE_BREAKPOINT_MARKER, 1)
                if i in cache_set and stable.strip():
                    blocks = [{
                        "type": "text",
                        "text": stable,
                        "cache_control": {"type": "ephemeral"},
                    }]
                    if variable:
                        blocks.append({"type": "text", "text": variable})
                    out.append({"role": "user", "content": blocks})
                    continue
                # Not eligible (older than the cap) — flatten to plain text.
                out.append({"role": "user", "content": stable + variable})
                continue
            out.append(msg)
        return out

    async def _send_text(self, text: str) -> str:
        self._history.append({"role": "user", "content": text})
        messages = self._build_anthropic_messages()
        cache_breakpoints = sum(
            1 for m in messages
            if isinstance(m.get("content"), list)
            and any(isinstance(b, dict) and b.get("cache_control") for b in m["content"])
        )
        request_json = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
        }
        # Fable 5 / Opus 4.8 / 4.7 reject sampling params with a 400; only send
        # temperature on models that still accept it.
        if _supports_temperature(self.model):
            request_json["temperature"] = 0.2
        logger.info(
            "[%s] POST %s model=%s cache_breakpoints=%d",
            self.provider_label,
            self.endpoint,
            self.model,
            cache_breakpoints,
        )
        async with httpx.AsyncClient(timeout=llm_timeout()) as client:
            resp = await client.post(
                self.endpoint,
                headers=anthropic_headers(self.api_key),
                json=request_json,
            )

        if resp.status_code >= 400:
            raise RuntimeError(
                f"{self.provider_label} API error: {resp.status_code} {resp.text[:300]}"
            )

        data = resp.json()
        err_obj = data.get("error") if isinstance(data, dict) else None
        if isinstance(err_obj, dict):
            msg = str(err_obj.get("message") or "").strip()
            if msg:
                raise RuntimeError(f"{self.provider_label} error: {msg}")

        usage = data.get("usage") or {}
        cache_read = usage.get("cache_read_input_tokens")
        cache_write = usage.get("cache_creation_input_tokens")
        if cache_read or cache_write:
            logger.info(
                "[%s] cache: read=%s write=%s input=%s output=%s",
                self.provider_label,
                cache_read, cache_write,
                usage.get("input_tokens"), usage.get("output_tokens"),
            )

        content = data.get("content")
        out = ""
        if isinstance(content, list):
            parts: list[str] = []
            for entry in content:
                if isinstance(entry, str):
                    txt = entry.strip()
                elif isinstance(entry, dict):
                    txt = str(entry.get("text") or entry.get("content") or "").strip()
                else:
                    txt = ""
                if txt:
                    parts.append(txt)
            out = "\n".join(parts).strip()
        elif isinstance(content, str):
            out = content.strip()

        if not out:
            raise RuntimeError(
                f"{self.provider_label} returned empty content. Response: {str(data)[:300]}"
            )

        self._history.append({"role": "assistant", "content": out})
        return out
