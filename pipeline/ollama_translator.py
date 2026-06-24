"""Native Ollama /api/chat translator.

For local models served by Ollama (e.g. a qwen3.5 on a LAN MacBook). Two
reasons the generic OpenAI flavour isn't enough:
  • thinking models (Qwen3.x, DeepSeek-R1 family) think by default, and
    Ollama's OpenAI-compatible /v1 endpoint has no way to turn that off —
    the native /api/chat accepts ``"think": false``;
  • local inference is slow and unauthenticated: timeouts must be far more
    generous than for cloud APIs, and no API key is required.

We piggy-back on :class:`OpenRouterTrackTranslator` for all JSON parsing /
chunk-coercion logic and only swap the network call, mirroring how the
Anthropic-compat translator is built.
"""

from __future__ import annotations

import logging

import httpx

from pipeline.openrouter_translator import (
    CACHE_BREAKPOINT_MARKER,
    OpenRouterTrackTranslator,
)

logger = logging.getLogger(__name__)

# Local models on consumer hardware can take minutes on a long chunk.
OLLAMA_TIMEOUT = 600.0

# Ollama defaults num_ctx to ~4096 and SILENTLY truncates the front of the
# prompt past that — which eats the instruction block first. Our largest
# self-contained chunk prompt is ~4k tokens including output, so 16384 gives
# comfortable headroom; KV-cache cost at 16k is modest for the 8-12B models
# this path targets.
OLLAMA_NUM_CTX = 16384


def _normalize_ollama_chat_url(base_url: str) -> str:
    """Resolve the user's base URL to ``{root}/api/chat``.

    Idempotent for URLs already ending in /api/chat; strips a trailing
    ``/v1`` or ``/v1/chat/completions`` (typically left over from trying the
    OpenAI request format against the same Ollama server)."""
    raw = (base_url or "").strip()
    if not raw:
        return ""
    while raw.endswith("/"):
        raw = raw[:-1]
    if raw.endswith("/api/chat"):
        return raw
    if raw.endswith("/chat/completions"):
        raw = raw[: -len("/chat/completions")]
        while raw.endswith("/"):
            raw = raw[:-1]
    if raw.endswith("/v1"):
        raw = raw[: -len("/v1")]
        while raw.endswith("/"):
            raw = raw[:-1]
    if raw.endswith("/api"):
        return raw + "/chat"
    return raw + "/api/chat"


async def ollama_chat(
    base_url: str,
    model: str,
    messages: list[dict],
    temperature: float = 0.2,
    timeout: float = OLLAMA_TIMEOUT,
    num_ctx: int = OLLAMA_NUM_CTX,
    format: dict | None = None,
) -> str:
    """One non-streaming /api/chat round-trip. ``messages`` is OpenAI-style
    ``[{role, content}]``. Sends ``think: false`` so thinking models answer
    directly (faster, and no reasoning prose polluting JSON output); retries
    once without the parameter for models / older Ollama versions that
    reject it. ``format`` is an optional JSON schema (Ollama's native
    structured-output control — the equivalent of the OpenAI json_schema
    response format) that constrains weak local models to valid JSON."""
    endpoint = _normalize_ollama_chat_url(base_url)
    if not endpoint:
        raise RuntimeError("ollama: base URL is required")
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,
        "options": {"temperature": temperature, "num_ctx": num_ctx},
    }
    if format is not None:
        body["format"] = format
    # Generous read window (local inference is slow) but a short connect window
    # so an offline server fails fast instead of hanging for the full timeout.
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15.0)) as client:
        resp = await client.post(endpoint, json=body)
        if resp.status_code >= 400 and "think" in (resp.text or "").lower():
            logger.warning(
                "[ollama] %s rejected the think parameter (%d) — retrying without it",
                endpoint, resp.status_code,
            )
            body.pop("think", None)
            resp = await client.post(endpoint, json=body)
    if resp.status_code >= 400:
        raise RuntimeError(f"ollama API error: {resp.status_code} {resp.text[:300]}")
    data = resp.json()
    err = data.get("error") if isinstance(data, dict) else None
    if err:
        raise RuntimeError(f"ollama error: {str(err)[:300]}")
    content = str(((data.get("message") or {}).get("content")) or "").strip()
    if not content:
        raise RuntimeError(f"ollama returned empty content. Response: {str(data)[:300]}")
    return content


class OllamaTrackTranslator(OpenRouterTrackTranslator):
    """Ollama-native variant. Same parsing/coercion as the OpenAI flavour;
    swaps the network call for /api/chat with thinking disabled. No API key."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str = "",
        provider_label: str = "openai_compat:ollama",
    ):
        super().__init__(
            api_key=api_key or "",
            model=model,
            base_url=base_url,
            provider_label=provider_label,
        )
        self.endpoint = _normalize_ollama_chat_url(self.base_url)

    async def _send_text(self, text: str, *, json_schema: dict | None = None) -> str:
        # Stateless: every chunk prompt is self-contained (full instructions +
        # description + glossary + last-4 prior segments are rebuilt each
        # call), so the conversation history the cloud translators keep for
        # prompt caching is pure redundancy here. Resending it grows each
        # request unboundedly, and once it exceeds num_ctx Ollama silently
        # drops the FRONT of the prompt — the instruction block — first.
        messages = [{"role": "user", "content": text.replace(CACHE_BREAKPOINT_MARKER, "\n")}]
        logger.info("[%s] POST %s model=%s", self.provider_label, self.endpoint, self.model)
        # The base class passes an OpenAI-style {name, strict, schema} wrapper;
        # Ollama's ``format`` wants the bare schema object, so unwrap it.
        fmt = json_schema.get("schema") if isinstance(json_schema, dict) else None
        return await ollama_chat(self.base_url, self.model, messages, format=fmt)
