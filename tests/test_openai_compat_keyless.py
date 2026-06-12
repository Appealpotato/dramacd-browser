"""Keyless OpenAI-compatible backends (LM Studio, llama.cpp, vLLM, Ollama)
and the format-aware model fetcher."""
import asyncio
import sys
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routers.api import (
    _openai_compat_models_url,
    _parse_models_payload,
    _provider_has_key,
)
from pipeline.anthropic_compat_translator import anthropic_headers


class ModelsUrlTests(unittest.TestCase):
    def test_openai_format_appends_models(self):
        # LM Studio's natural base URL.
        self.assertEqual(
            _openai_compat_models_url("http://localhost:1234/v1", "openai"),
            "http://localhost:1234/v1/models",
        )

    def test_strips_chat_completions_suffix(self):
        self.assertEqual(
            _openai_compat_models_url("http://x/v1/chat/completions", "openai"),
            "http://x/v1/models",
        )

    def test_anthropic_strips_messages_suffix(self):
        self.assertEqual(
            _openai_compat_models_url("http://localhost:42069/v1/messages", "anthropic"),
            "http://localhost:42069/v1/models",
        )

    def test_ollama_uses_native_tags(self):
        self.assertEqual(
            _openai_compat_models_url("http://192.168.1.125:11434", "ollama"),
            "http://192.168.1.125:11434/api/tags",
        )

    def test_ollama_tolerates_v1_or_api_suffixes(self):
        for base in (
            "http://h:11434/v1",
            "http://h:11434/api",
            "http://h:11434/api/chat",
            "http://h:11434/",
        ):
            self.assertEqual(
                _openai_compat_models_url(base, "ollama"), "http://h:11434/api/tags"
            )


class ParseModelsPayloadTests(unittest.TestCase):
    def test_openai_shape(self):
        payload = {"data": [{"id": "qwen3.5-9b"}, {"id": "gemma4:12b"}]}
        self.assertEqual(
            _parse_models_payload(payload, "openai"), ["gemma4:12b", "qwen3.5-9b"]
        )

    def test_ollama_shape(self):
        payload = {"models": [{"name": "gemma4:12b"}, {"name": "translategemma:12b"}]}
        self.assertEqual(
            _parse_models_payload(payload, "ollama"),
            ["gemma4:12b", "translategemma:12b"],
        )

    def test_cross_shape_fallback(self):
        # An OpenAI-compat server that returns {"models": [...]} anyway.
        payload = {"models": [{"id": "m1"}]}
        self.assertEqual(_parse_models_payload(payload, "openai"), ["m1"])

    def test_bare_list_and_strings(self):
        self.assertEqual(_parse_models_payload(["b", "a", "a"], "openai"), ["a", "b"])

    def test_garbage(self):
        self.assertEqual(_parse_models_payload(None, "openai"), [])
        self.assertEqual(_parse_models_payload({"data": "nope"}, "openai"), [])


class KeylessProviderTests(unittest.TestCase):
    """openai_compat is fully configured with base URL + model alone — the
    key is optional for every request format (local servers are keyless)."""

    def test_has_key_without_api_key(self):
        async def run():
            with patch("routers.api.db.get_runtime_openai_compat_base_url", new=AsyncMock(return_value="http://localhost:1234/v1")), \
                 patch("routers.api.db.get_runtime_openai_compat_model", new=AsyncMock(return_value="some-model")):
                return await _provider_has_key("openai_compat")

        self.assertTrue(asyncio.run(run()))

    def test_not_configured_without_base_url(self):
        async def run():
            with patch("routers.api.db.get_runtime_openai_compat_base_url", new=AsyncMock(return_value="")), \
                 patch("routers.api.db.get_runtime_openai_compat_model", new=AsyncMock(return_value="some-model")):
                return await _provider_has_key("openai_compat")

        self.assertFalse(asyncio.run(run()))

    def test_translation_job_key_optional_any_format(self):
        # The job-side gate mirrors _provider_has_key: openai_compat never
        # hard-requires a key, regardless of request format. Guard against
        # the old ollama-only exemption regressing.
        import inspect
        import pipeline.translation_job as tj

        src = inspect.getsource(tj._run_translation_job_inner)
        self.assertIn('_key_optional = provider == "openai_compat"', src)
        self.assertNotIn('runtime_request_format == "ollama"\n    if not runtime_api_key', src)


class AnthropicHeadersTests(unittest.TestCase):
    def test_with_key(self):
        headers = anthropic_headers("sk-test")
        self.assertEqual(headers["x-api-key"], "sk-test")
        self.assertIn("anthropic-version", headers)

    def test_keyless_omits_header_entirely(self):
        # httpx raises on None header values — keyless must omit, not null.
        headers = anthropic_headers(None)
        self.assertNotIn("x-api-key", headers)
        self.assertIn("anthropic-version", headers)
        self.assertNotIn(None, headers.values())


if __name__ == "__main__":
    unittest.main()
