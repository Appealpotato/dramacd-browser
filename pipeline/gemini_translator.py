import json
import logging
from math import ceil

import httpx

from config import llm_timeout

logger = logging.getLogger(__name__)


def estimate_tokens(text: str) -> int:
    # Rough multilingual heuristic for chunk budgeting.
    if not text:
        return 1
    return max(1, ceil(len(text) / 4))


def build_token_chunks(segments: list[dict], max_tokens: int, max_lines: int) -> list[list[dict]]:
    chunks: list[list[dict]] = []
    current: list[dict] = []
    token_count = 0

    for seg in segments:
        source = str(seg.get("source_text") or "")
        seg_tokens = estimate_tokens(source)
        next_over_budget = current and (token_count + seg_tokens > max_tokens or len(current) >= max_lines)
        if next_over_budget:
            chunks.append(current)
            current = []
            token_count = 0

        current.append(seg)
        token_count += seg_tokens

    if current:
        chunks.append(current)
    return chunks


class GeminiTrackTranslator:
    def __init__(self, *, api_key: str, model: str):
        self.api_key = api_key
        self.model = model
        self._history: list[dict] = []

    def _url(self) -> str:
        return f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"

    async def _send_text(self, text: str) -> str:
        self._history.append({"role": "user", "parts": [{"text": text}]})
        async with httpx.AsyncClient(timeout=llm_timeout()) as client:
            resp = await client.post(
                self._url(),
                params={"key": self.api_key},
                json={
                    "contents": self._history,
                    "generationConfig": {
                        "temperature": 0.2,
                        "responseMimeType": "application/json",
                    },
                },
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"Gemini API error: {resp.status_code} {resp.text[:300]}")

        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            raise RuntimeError("Gemini returned no candidates")
        parts = ((candidates[0].get("content") or {}).get("parts") or [])
        if not parts:
            raise RuntimeError("Gemini returned empty content")
        out = str(parts[0].get("text") or "").strip()
        self._history.append({"role": "model", "parts": [{"text": out}]})
        return out

    @staticmethod
    def _extract_json_array(raw: str):
        # Shared chain: think-strip, fence-strip, strict parse, unescaped-
        # inner-quote repair, outermost-span fallback ([...] and {...}).
        from pipeline.json_extract import loads_robust
        try:
            return loads_robust(raw)
        except ValueError as e:
            logger.warning(f"[GeminiTrackTranslator] JSON parse failed: {e}")
            raise

    async def translate_text_to_en(self, text: str) -> str:
        prompt = (
            "Translate this Japanese drama CD description to natural English. "
            "Return JSON object only: {\"text\":\"...\"}\n\n"
            f"Text:\n{text}"
        )
        raw = await self._send_text(prompt)
        parsed = self._extract_json_array(raw)
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
            f"Prior segment context (last 4): {json.dumps(prior_payload, ensure_ascii=False)}\n\n"
            f"SEGMENTS TO TRANSLATE:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
            "Return the JSON array only:"
        )

        logger.info(f"[GeminiTrackTranslator] Translating {len(chunk_segments)} segments to {target_language}")
        raw = await self._send_text(prompt)
        logger.debug(f"[GeminiTrackTranslator] Raw response (first 400 chars): {raw[:400]}")

        try:
            parsed = self._extract_json_array(raw)
        except Exception as e:
            logger.error(f"[GeminiTrackTranslator] Failed to parse JSON: {e}")
            logger.error(f"[GeminiTrackTranslator] Raw response was: {raw[:500]}")
            raise

        if not isinstance(parsed, list):
            logger.warning(f"[GeminiTrackTranslator] Response not a list, got {type(parsed).__name__}")
            raise RuntimeError("Chunk translation did not return a JSON array")

        rows = []
        for row in parsed:
            if not isinstance(row, dict):
                logger.debug(f"[GeminiTrackTranslator] Skipping non-dict row: {type(row).__name__}")
                continue
            if "segment_index" not in row:
                logger.debug(f"[GeminiTrackTranslator] Row missing segment_index key: {list(row.keys())}")
                continue
            try:
                rows.append(
                    {
                        "segment_index": int(row["segment_index"]),
                        "text": str(row.get("text") or "").strip(),
                        "meta": {"provider": "gemini", "model": self.model},
                    }
                )
            except Exception as e:
                logger.warning(f"[GeminiTrackTranslator] Failed to process row: {e}, row={row}")

        logger.info(f"[GeminiTrackTranslator] Processed {len(rows)} valid rows from {len(parsed)} parsed items")
        return rows
