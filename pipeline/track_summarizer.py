import json
import logging

logger = logging.getLogger(__name__)


class TrackSummarizer:
    """
    Generates structured summaries of transcribed drama CD tracks for context memory.
    Summaries help maintain continuity, correct anatomy/slang interpretation across tracks.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        provider: str = "gemini",
        base_url: str | None = None,
        request_format: str = "openai",
    ):
        self.api_key = api_key
        self.model = model
        self.provider = provider.lower()
        self.base_url = (base_url or "").strip().rstrip("/") or None
        self.request_format = (request_format or "openai").lower()

    async def generate_summary(
        self,
        *,
        track_number: int,
        segments: list[dict],
        drama_description: str = "",
        previous_summaries: list[dict] = None,
    ) -> dict:
        """
        Generate a structured JSON summary of a transcribed track.

        Args:
            track_number: Track number (1-indexed)
            segments: List of segment dicts with 'text' field
            drama_description: Optional context about the drama CD

        Returns:
            dict with summary structure, or None if generation fails
        """
        # Build full transcript text
        full_text = "\n".join([str(seg.get("text", "")).strip() for seg in segments if str(seg.get("text", "")).strip()])

        if not full_text:
            logger.warning(f"[TrackSummarizer] No text to summarize for track {track_number}")
            return None

        prompt = self._build_summary_prompt(
             track_number,
            full_text,
            drama_description,
            previous_summaries or []
            )
        try:
            if self.provider == "gemini":
                summary_json_str = await self._call_gemini(prompt)
            elif self.provider == "openrouter":
                summary_json_str = await self._call_openrouter(prompt)
            elif self.provider == "chutes":
                summary_json_str = await self._call_chutes(prompt)
            elif self.provider == "openai_compat":
                summary_json_str = await self._call_openai_compat(prompt)
            else:
                raise ValueError(f"Unsupported provider: {self.provider}")

            # Parse and validate JSON
            summary = self._extract_and_validate_json(summary_json_str)
            logger.info(f"[TrackSummarizer] Generated summary for track {track_number}: {len(summary_json_str)} chars")
            return summary

        except Exception as e:
            logger.error(f"[TrackSummarizer] Failed to generate summary for track {track_number}: {e}")
            return None

    def _build_summary_prompt(
        self,
        track_number: int,
        transcript_text: str,
        drama_description: str,
        previous_summaries: list[dict]
    ) -> str:

        # Build continuity section
        context_section = ""
        if previous_summaries:
            context_section += (
                "Here are summaries of the previous tracks to maintain emotional and narrative continuity:\n"
            )
            for s in previous_summaries[-2:]:  # Only last 2 tracks
                tn = s.get("track_number")
                ss = s.get("scene_summary", "").strip()
                context_section += f"- Track {tn}: {ss}\n"
            context_section += "\n"

        # Main prompt
        return (
            f"You are summarizing a transcribed drama CD track for translation context memory.\n\n"
            f"Drama CD description: {drama_description or '(none)'}\n\n"
            f"{context_section}"
            f"Track {track_number} transcript:\n{transcript_text}\n\n"
            f"Generate a concise JSON summary (200-400 tokens max) with this structure:\n"
            f"{{\n"
            f'  "track_number": {track_number},\n'
            f'  "scene_summary": "1-3 sentence summary of what happened",\n'
            f'  "listener_state": "female; emotional + physical state",\n'
            f'  "boyfriend_state": "emotional + behavioral state",\n'
            f'  "relationship_state": "brief description of the dynamic in this track",\n'
            f'  "important_terms": {{\n'
            f'    "slang_or_term": "interpretation note (e.g. ぼっき used metaphorically for female arousal)"\n'
            f'  }}\n'
            f"}}\n\n"
            f"Guidelines:\n"
            f"• Keep it concise (200-400 tokens)\n"
            f"• Focus on emotional/physical states and relationship dynamics\n"
            f"• Note any slang/metaphor interpretation\n"
            f"• Listener is female\n\n"
            f"Respond ONLY with valid JSON (no markdown, no code blocks):"
        )

    async def _call_gemini(self, prompt: str) -> str:
        """Call Gemini API to generate summary."""
        import httpx

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                url,
                params={"key": self.api_key},
                json={
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "temperature": 0.3,
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

        return str(parts[0].get("text") or "").strip()

    async def _call_openrouter(self, prompt: str) -> str:
        """Call OpenRouter API to generate summary."""
        import httpx

        url = "https://openrouter.ai/api/v1/chat/completions"

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                    "response_format": {"type": "json_object"},
                },
            )

        if resp.status_code >= 400:
            raise RuntimeError(f"OpenRouter API error: {resp.status_code} {resp.text[:300]}")

        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("OpenRouter returned no choices")

        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        return content.strip()

    async def _call_chutes(self, prompt: str) -> str:
        """Call Chutes API to generate summary."""
        import httpx

        url = "https://api.chutes.ai/v1/chat/completions"

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                    "response_format": {"type": "json_object"},
                },
            )

        if resp.status_code >= 400:
            raise RuntimeError(f"Chutes API error: {resp.status_code} {resp.text[:300]}")

        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("Chutes returned no choices")

        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        return content.strip()

    async def _call_openai_compat(self, prompt: str) -> str:
        """Call a generic OpenAI-compatible endpoint, branching on whether the
        proxy expects OpenAI Chat Completions or Anthropic Messages format."""
        import httpx

        if not self.base_url:
            raise RuntimeError("openai_compat summarizer needs a base URL")

        if self.request_format == "anthropic":
            from pipeline.anthropic_compat_translator import (
                _normalize_messages_url,
                _supports_temperature,
                ANTHROPIC_VERSION,
                DEFAULT_MAX_TOKENS,
            )
            endpoint = _normalize_messages_url(self.base_url)
            anthropic_body = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": DEFAULT_MAX_TOKENS,
            }
            # Fable 5 / Opus 4.8 / 4.7 reject sampling params with a 400; only
            # send temperature on models that still accept it.
            if _supports_temperature(self.model):
                anthropic_body["temperature"] = 0.3
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    endpoint,
                    headers={
                        "Content-Type": "application/json",
                        "x-api-key": self.api_key,
                        "anthropic-version": ANTHROPIC_VERSION,
                    },
                    json=anthropic_body,
                )
            if resp.status_code >= 400:
                raise RuntimeError(f"openai_compat (anthropic) error: {resp.status_code} {resp.text[:300]}")
            data = resp.json()
            content = data.get("content")
            if isinstance(content, list):
                parts = []
                for entry in content:
                    if isinstance(entry, dict):
                        txt = str(entry.get("text") or "").strip()
                        if txt:
                            parts.append(txt)
                return "\n".join(parts).strip()
            return str(content or "").strip()

        # OpenAI Chat Completions flavour with response_format fallback.
        from pipeline.openrouter_translator import _normalize_chat_completions_url
        endpoint = _normalize_chat_completions_url(self.base_url)
        last_err = ""
        for use_response_format in (True, False):
            body = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            }
            if use_response_format:
                body["response_format"] = {"type": "json_object"}
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
            if resp.status_code >= 400:
                last_err = f"{resp.status_code} {resp.text[:200]}"
                if use_response_format:
                    continue
                raise RuntimeError(f"openai_compat (openai) error: {last_err}")
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                if use_response_format:
                    continue
                raise RuntimeError("openai_compat returned no choices")
            message = choices[0].get("message") or {}
            content = message.get("content") or ""
            return str(content).strip()
        raise RuntimeError(f"openai_compat exhausted retries: {last_err}")

    @staticmethod
    def _extract_and_validate_json(raw: str) -> dict:
        """Extract and validate JSON from model response."""
        payload = raw.strip()

        # Strip markdown code blocks if present
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

        # Parse JSON
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as e:
            # Try extracting JSON from text
            first_brace = payload.find("{")
            last_brace = payload.rfind("}")
            if first_brace >= 0 and last_brace > first_brace:
                payload = payload[first_brace:last_brace + 1]
                parsed = json.loads(payload)
            else:
                raise RuntimeError(f"Failed to parse JSON: {e}")

        # Validate required fields
        if not isinstance(parsed, dict):
            raise RuntimeError(f"Summary must be a JSON object, got {type(parsed).__name__}")

        required_fields = ["track_number", "scene_summary", "listener_state", "boyfriend_state", "relationship_state"]
        for field in required_fields:
            if field not in parsed:
                logger.warning(f"[TrackSummarizer] Missing required field '{field}' in summary")

        return parsed
