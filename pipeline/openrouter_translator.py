import json
import logging
import re

import httpx

from config import llm_timeout

logger = logging.getLogger(__name__)


# Inserted into chunk-translation prompts between the cacheable prefix
# (instructions + drama context + summaries + glossary + character memory)
# and the per-chunk variable suffix (prior context + segments to translate).
# OpenAI-flavoured callers strip it; the Anthropic translator splits on it
# and applies cache_control to the prefix block.
CACHE_BREAKPOINT_MARKER = "\n[[__CACHE_BREAKPOINT__]]\n"


def _normalize_chat_completions_url(base_url: str) -> str:
    """Resolve a user-provided base URL to the full /chat/completions endpoint.

    The full base URL is preserved verbatim. The function only appends
    '/chat/completions' to it (after stripping trailing slashes) — every other
    path segment, including any '/v1' or vendor-specific prefix the user typed,
    is kept exactly as supplied.

    Accepted inputs (each maps to the value on the right):
      'https://host/v1'                       -> 'https://host/v1/chat/completions'
      'https://host/v1/'                      -> 'https://host/v1/chat/completions'
      'https://host/api/v2/openai'            -> 'https://host/api/v2/openai/chat/completions'
      'https://host/v1/chat/completions'      -> 'https://host/v1/chat/completions'  (idempotent)
      'https://host'                          -> 'https://host/chat/completions'
    """
    raw = (base_url or "").strip()
    if not raw:
        return ""
    # Strip ONLY trailing slashes — never any path component.
    while raw.endswith("/"):
        raw = raw[:-1]
    suffix = "/chat/completions"
    # Idempotent: don't double-append if the user already pasted the full URL.
    if raw.endswith(suffix):
        return raw
    return raw + suffix


class OpenRouterTrackTranslator:
    DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

    # JSON-schema response formats, used only as a fallback when a server
    # rejects the json_object response_format (newer LM Studio / vLLM answer
    # such requests with 400 "'response_format.type' must be 'json_schema' or
    # 'text'"). OpenAI strict json_schema requires the root to be an OBJECT,
    # not a bare array, so the segment list is wrapped under "segments" —
    # _coerce_rows_payload already unwraps that key transparently.
    CHUNK_RESPONSE_SCHEMA = {
        "name": "drama_translation",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "segments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "segment_index": {"type": "integer"},
                            "text": {"type": "string"},
                        },
                        "required": ["segment_index", "text"],
                    },
                },
            },
            "required": ["segments"],
        },
    }
    TEXT_RESPONSE_SCHEMA = {
        "name": "translation",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    }

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        provider_label: str = "openrouter",
        stateless: bool = False,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = (base_url or self.DEFAULT_BASE_URL).strip().rstrip("/") or self.DEFAULT_BASE_URL
        self.endpoint = _normalize_chat_completions_url(self.base_url)
        self.provider_label = provider_label
        # Stateless: send only the current (self-contained) chunk prompt, never
        # a growing conversation. Required for local servers (LM Studio /
        # llama.cpp / vLLM) whose fixed context window would otherwise overflow
        # a few chunks into a long track — same reasoning as the Ollama path.
        # Cloud providers keep history for prompt caching.
        self._stateless = stateless
        self._history: list[dict] = []
        # The response_format that last succeeded against this server, learned
        # on the first chunk and tried first on every later chunk so we don't
        # re-pay a doomed negotiation (e.g. LM Studio's json_object 400) per
        # chunk. None until the first successful round-trip.
        self._preferred_rf_type: str | None = None

    @staticmethod
    def _extract_message_content(message) -> str:
        if isinstance(message, str):
            return message.strip()
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for entry in content:
                if isinstance(entry, str):
                    txt = entry.strip()
                    if txt:
                        parts.append(txt)
                elif isinstance(entry, dict):
                    txt = str(entry.get("text") or entry.get("content") or "").strip()
                    if txt:
                        parts.append(txt)
            return "\n".join(parts).strip()
        return ""

    @staticmethod
    def _is_context_length_error(text: str) -> bool:
        """A 400 about the prompt exceeding the model's context window — NOT a
        response_format rejection, so falling through to another format won't
        help. Recognise the common phrasings across local servers."""
        t = (text or "").lower()
        return any(
            k in t for k in (
                "context size", "context length", "context window",
                "maximum context", "context_length_exceeded",
                "too many tokens", "exceeds the model",
            )
        )

    async def _send_text(self, text: str, *, json_schema: dict | None = None) -> str:
        if self._stateless:
            convo = [{"role": "user", "content": text}]
        else:
            self._history.append({"role": "user", "content": text})
            convo = self._history

        # Response formats we know how to ask for, tried in order until one is
        # accepted:
        #   json_object  — the OpenAI standard; OpenRouter / Chutes and older
        #                  LM Studio builds honour it.
        #   json_schema  — newer LM Studio / vLLM REJECT json_object with 400
        #                  ("'response_format.type' must be 'json_schema' or
        #                  'text'"); a schema still constrains weak local
        #                  models to valid JSON, so prefer it over free text.
        #   none         — last-resort unconstrained text; the caller's robust
        #                  JSON extraction has to cope.
        candidates: dict[str, dict | None] = {"json_object": {"type": "json_object"}}
        if json_schema is not None:
            candidates["json_schema"] = {"type": "json_schema", "json_schema": json_schema}
        candidates["none"] = None

        order = list(candidates.keys())
        # Once a format has worked for this translator (this job / server), try
        # it FIRST on every later chunk — otherwise a server that rejects
        # json_object would eat a wasted 400 on every single chunk.
        if self._preferred_rf_type in candidates:
            order.remove(self._preferred_rf_type)
            order.insert(0, self._preferred_rf_type)

        last_detail = ""
        for idx, rf_name in enumerate(order):
            response_format = candidates[rf_name]
            is_last = idx == len(order) - 1
            # OpenAI-style endpoints don't understand our cache breakpoint
            # marker; strip it so it never reaches the wire.
            cleaned = [
                {**m, "content": m["content"].replace(CACHE_BREAKPOINT_MARKER, "\n")}
                if isinstance(m.get("content"), str) else m
                for m in convo
            ]
            request_json = {
                "model": self.model,
                "messages": cleaned,
                "temperature": 0.2,
            }
            if response_format is not None:
                request_json["response_format"] = response_format

            logger.info(
                "[%s] POST %s model=%s response_format=%s",
                self.provider_label,
                self.endpoint,
                self.model,
                rf_name,
            )
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            async with httpx.AsyncClient(timeout=llm_timeout()) as client:
                resp = await client.post(
                    self.endpoint,
                    headers=headers,
                    json=request_json,
                )

            label = self.provider_label
            if resp.status_code >= 400:
                # A context-window overflow is not a format problem — trying
                # another response_format just fails again with a confusing
                # "rejected response_format" warning. Surface it directly with
                # an actionable hint instead.
                if self._is_context_length_error(resp.text):
                    raise RuntimeError(
                        f"{label}: chunk too large for the model's context window "
                        f"({resp.status_code}: {resp.text[:200]}). Lower 'Max tokens "
                        f"per chunk' in Settings, or raise the server's context size."
                    )
                # Some OpenAI-compatible servers reject a given response_format
                # (Anthropic shims reject them all; newer LM Studio rejects
                # json_object specifically). Fall through to the next format.
                if not is_last:
                    logger.warning(
                        "[%s] %s rejected response_format=%s (%d): %s — retrying with %s",
                        label, self.endpoint, rf_name,
                        resp.status_code, resp.text[:200], order[idx + 1],
                    )
                    last_detail = f"{resp.status_code} {resp.text[:200]}"
                    continue
                raise RuntimeError(f"{label} API error: {resp.status_code} {resp.text[:300]}")

            data = resp.json()
            err_obj = data.get("error") if isinstance(data, dict) else None
            if isinstance(err_obj, dict):
                msg = str(err_obj.get("message") or "").strip()
                if msg:
                    raise RuntimeError(f"{label} error: {msg}")

            choices = data.get("choices") or []
            if not choices:
                last_detail = str(data)[:300]
                if not is_last:
                    continue
                raise RuntimeError(f"{label} returned no choices. Response: {last_detail}")

            message = choices[0].get("message") or {}
            out = self._extract_message_content(message)
            if out:
                # Remember the winning format so later chunks lead with it.
                self._preferred_rf_type = rf_name
                if not self._stateless:
                    self._history.append({"role": "assistant", "content": out})
                return out

            last_detail = str(choices[0])[:300]
            if not is_last:
                continue
            raise RuntimeError(f"{label} returned empty content. Choice: {last_detail}")

        raise RuntimeError(f"{self.provider_label} returned no usable output")

    @staticmethod
    def _extract_json(raw: str):
        # Shared chain: think-strip, fence-strip, strict parse, unescaped-
        # inner-quote repair, outermost-span fallback.
        from pipeline.json_extract import loads_robust
        return loads_robust(raw)

    @staticmethod
    def _coerce_rows_payload(parsed):
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            # Map-style payload: {"1":"...", "2":"..."}
            numeric_pairs = []
            for key, value in parsed.items():
                try:
                    seg_idx = int(str(key).strip())
                except Exception:
                    continue
                if isinstance(value, dict):
                    text = OpenRouterTrackTranslator._row_text(value)
                else:
                    text = str(value or "").strip()
                if text:
                    numeric_pairs.append({"segment_index": seg_idx, "text": text})
            if numeric_pairs:
                numeric_pairs.sort(key=lambda x: x["segment_index"])
                return numeric_pairs
            for key in ("translated_segments", "translations", "segments", "results", "items", "data"):
                maybe = parsed.get(key)
                if isinstance(maybe, list):
                    return maybe
            if "segment_index" in parsed and ("text" in parsed or "translation" in parsed):
                return [parsed]
            nested = parsed.get("output") or parsed.get("json")
            if isinstance(nested, str):
                try:
                    nested_parsed = OpenRouterTrackTranslator._extract_json(nested)
                    return OpenRouterTrackTranslator._coerce_rows_payload(nested_parsed)
                except Exception:
                    pass
        return None

    @staticmethod
    def _row_text(row: dict) -> str:
        return str(
            row.get("text")
            or row.get("translation")
            or row.get("translated_text")
            or row.get("content")
            or row.get("output")
            or row.get("value")
            or ""
        ).strip()

    @staticmethod
    def _row_index(row: dict):
        return row.get(
            "segment_index",
            row.get(
                "segmentIndex",
                row.get("index", row.get("id", row.get("line", row.get("line_number")))),
            ),
        )

    @staticmethod
    def _extract_json_candidate(raw: str):
        text = str(raw or "").strip()
        first_arr = text.find("[")
        last_arr = text.rfind("]")
        if first_arr >= 0 and last_arr > first_arr:
            candidate = text[first_arr:last_arr + 1]
            try:
                return json.loads(candidate)
            except Exception:
                pass
        first_obj = text.find("{")
        last_obj = text.rfind("}")
        if first_obj >= 0 and last_obj > first_obj:
            candidate = text[first_obj:last_obj + 1]
            try:
                return json.loads(candidate)
            except Exception:
                pass
        return None

    @staticmethod
    def _coerce_rows_from_lines(raw: str):
        rows = []
        pattern = re.compile(r"^\s*(?:\#|\[)?(\d{1,6})(?:\]|\)|:|-|\.)\s*(.+?)\s*$")
        for line in str(raw or "").splitlines():
            line = line.strip()
            if not line:
                continue
            m = pattern.match(line)
            if not m:
                continue
            rows.append({"segment_index": int(m.group(1)), "text": m.group(2).strip()})
        return rows if rows else None

    async def _repair_chunk_output_to_json(self, raw_output: str) -> list | None:
        prompt = (
            "Convert the following model output into strict JSON array only.\n"
            "Output format: [{\"segment_index\":123,\"text\":\"...\"}]\n"
            "Do not add commentary.\n\n"
            f"Input:\n{raw_output}"
        )
        repaired_raw = await self._send_text(prompt, json_schema=self.CHUNK_RESPONSE_SCHEMA)
        try:
            repaired_parsed = self._extract_json(repaired_raw)
        except Exception:
            repaired_parsed = self._extract_json_candidate(repaired_raw)
        return self._coerce_rows_payload(repaired_parsed)

    async def translate_text_to_en(self, text: str) -> str:
        prompt = (
            "Translate this Japanese drama CD description to natural English. "
            "Return JSON object only: {\"text\":\"...\"}\n\n"
            f"Text:\n{text}"
        )
        raw = await self._send_text(prompt, json_schema=self.TEXT_RESPONSE_SCHEMA)
        parsed = self._extract_json(raw)
        if isinstance(parsed, dict) and parsed.get("text"):
            return str(parsed["text"]).strip()
        raise RuntimeError("Description translation response did not contain text")

    async def translate_chunk(
        self,
        *,
        target_language: str,
        description_context: str,
        chunk_segments: list[dict],
        prior_context: list[dict],
        glossary: str = "",
        character_memory: str = "",
        previous_summaries: list[dict] = None,
    ) -> list[dict]:
        payload = [
            {"segment_index": int(seg["segment_index"]), "text": str(seg["source_text"])}
            for seg in chunk_segments
        ]
        prior_payload = [
            {"segment_index": int(seg["segment_index"]), "text": str(seg["text"])}
            for seg in prior_context[-4:]
        ]
        prompt = (
            "You are translating Japanese drama CD transcript segments to " + target_language + ".\n"
            "Your role: Preserve meaning, emotion, and character voice while creating natural, idiomatic translations.\n\n"
            "CRITICAL REQUIREMENTS:\n"
            "1. Respond ONLY with valid JSON array: [{\"segment_index\":N,\"text\":\"...\"}]\n"
            "2. Preserve segment_index values exactly as provided.\n"
            "3. Return exactly one translation per input segment.\n"
            "4. No markdown, no code blocks, no commentary—just pure JSON.\n\n"
            "TRANSLATION GUIDELINES:\n"
            "• Prioritize natural, idiomatic language over literal translation.\n"
            "• Preserve character voice: maintain each speaker's tone, formality level, and personality.\n"
            "• Handle context: Use prior segments and drama description to understand character relationships and emotional subtext.\n"
            "• Ambiguity: When multiple interpretations exist, choose the most contextually appropriate one.\n"
            "• Consistency: Use the glossary and character memory to maintain consistent terminology and characterization.\n\n"
            "SEXUAL CONTENT & ANATOMY RULES (CRITICAL):\n"
            "When translating erotic content:\n"
            "1. The listener/heroine is female. Always translate arousal language accordingly.\n"
            "2. When Japanese uses masculine-coded slang (e.g., ぼっき) metaphorically for female arousal, DO NOT translate it literally as \"you're hard.\" Instead, render it as:\n"
            "   • \"you're so turned on\"\n"
            "   • \"you're this aroused\"\n"
            "   • \"you're swollen with arousal\"\n"
            "   • \"you're this sensitive\"\n"
            "3. Never introduce male-genital interpretations unless explicitly present in the original text.\n"
            "4. Avoid porn-hallucinatory embellishments not grounded in the Japanese.\n"
            "5. Preserve emotional intensity without adding new sexual acts or fluids.\n\n"
            f"CONTEXT:\n"
            f"Drama description (translated): {description_context or '(none)'}\n"
        )

        # Add previous track summaries if available (N-1, N-2)
        if previous_summaries:
            prompt += "Previous track context summaries:\n"
            for summary_data in previous_summaries:
                track_idx = summary_data.get("track_index", "?")
                summary_json = summary_data.get("summary_json", "{}")
                prompt += f"Track {track_idx}: {summary_json}\n"
            prompt += "\n"
        else:
            prompt += "Previous track summaries: (none)\n\n"

        prompt += (
            f"Preferred terms / glossary: {glossary or '(none)'}\n"
            f"Character notes (personality, speech style): {character_memory or '(none)'}\n"
            # Cache breakpoint: everything above is identical across chunks of
            # the same translation session and is the prefix Anthropic should
            # cache. Below this line varies per chunk.
            f"{CACHE_BREAKPOINT_MARKER}"
            f"Prior segment context (last 4): {json.dumps(prior_payload, ensure_ascii=False)}\n\n"
            f"SEGMENTS TO TRANSLATE:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
            "Return the JSON array only:"
        )
        logger.info(f"[OpenRouterTrackTranslator] Translating {len(chunk_segments)} segments to {target_language}")
        raw = await self._send_text(prompt, json_schema=self.CHUNK_RESPONSE_SCHEMA)
        logger.debug(f"[OpenRouterTrackTranslator] Raw response (first 400 chars): {str(raw)[:400]}")

        parsed = None
        try:
            parsed = self._extract_json(raw)
        except Exception as e:
            logger.debug(f"[OpenRouterTrackTranslator] _extract_json failed: {e}, trying _extract_json_candidate")
            parsed = self._extract_json_candidate(raw)

        rows_raw = self._coerce_rows_payload(parsed)
        logger.debug(f"[OpenRouterTrackTranslator] After _coerce_rows_payload: {type(rows_raw).__name__}, len={len(rows_raw) if isinstance(rows_raw, list) else 'N/A'}")

        if not isinstance(rows_raw, list):
            rows_raw = self._coerce_rows_from_lines(raw)
            logger.debug(f"[OpenRouterTrackTranslator] After _coerce_rows_from_lines: {type(rows_raw).__name__}, len={len(rows_raw) if isinstance(rows_raw, list) else 'N/A'}")

        if not isinstance(rows_raw, list):
            rows_raw = await self._repair_chunk_output_to_json(raw)
            logger.debug(f"[OpenRouterTrackTranslator] After _repair_chunk_output_to_json: {type(rows_raw).__name__}, len={len(rows_raw) if isinstance(rows_raw, list) else 'N/A'}")

        if not isinstance(rows_raw, list):
            snippet = str(raw).replace("\n", " ")[:220]
            logger.error(f"[OpenRouterTrackTranslator] Failed to parse output: {snippet}")
            raise RuntimeError(f"Chunk translation did not return a JSON array. Raw snippet: {snippet}")

        rows = []
        for i, row in enumerate(rows_raw):
            if isinstance(row, str):
                text = row.strip()
                if text:
                    logger.warning(f"[OpenRouterTrackTranslator] Row {i} is string (no segment_index): {text[:100]}")
                    # Keep indexless rows; translation_job's alignment fallbacks map them positionally.
                    rows.append({"text": text, "meta": {"provider": self.provider_label, "model": self.model}})
                continue
            if not isinstance(row, dict):
                logger.debug(f"[OpenRouterTrackTranslator] Skipping non-dict row {i}: {type(row).__name__}")
                continue
            text = self._row_text(row)
            if not text:
                logger.debug(f"[OpenRouterTrackTranslator] Row {i} has no text, keys: {list(row.keys())}")
                continue
            seg_idx = self._row_index(row)
            parsed_idx = None
            if seg_idx is not None:
                try:
                    parsed_idx = int(seg_idx)
                except Exception:
                    logger.warning(f"[OpenRouterTrackTranslator] Row {i} has non-int segment_index: {seg_idx}")
                    parsed_idx = None

            normalized = {
                "text": text,
                "meta": {"provider": self.provider_label, "model": self.model},
            }
            if parsed_idx is not None:
                normalized["segment_index"] = parsed_idx
            else:
                # Keep indexless rows; translation_job's alignment fallbacks map them positionally.
                logger.warning(f"[OpenRouterTrackTranslator] Row {i} has no valid segment_index, keys: {list(row.keys())} (kept for positional alignment)")
            rows.append(normalized)

        logger.info(f"[OpenRouterTrackTranslator] Processed {len(rows)} rows ({sum(1 for r in rows if 'segment_index' in r)} with segment_index)")
        return rows
