"""Ollama request format — URL normalization, settings validation, context window."""
import asyncio
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database
from pipeline.ollama_translator import (
    OLLAMA_NUM_CTX,
    OllamaTrackTranslator,
    _normalize_ollama_chat_url,
    ollama_chat,
)


class NormalizeOllamaUrlTests(unittest.TestCase):
    def test_bare_host(self):
        self.assertEqual(
            _normalize_ollama_chat_url("http://192.168.1.125:11434"),
            "http://192.168.1.125:11434/api/chat",
        )

    def test_trailing_slash(self):
        self.assertEqual(
            _normalize_ollama_chat_url("http://localhost:11434/"),
            "http://localhost:11434/api/chat",
        )

    def test_v1_leftover_from_openai_mode(self):
        self.assertEqual(
            _normalize_ollama_chat_url("http://localhost:11434/v1"),
            "http://localhost:11434/api/chat",
        )

    def test_full_openai_path_leftover(self):
        self.assertEqual(
            _normalize_ollama_chat_url("http://localhost:11434/v1/chat/completions"),
            "http://localhost:11434/api/chat",
        )

    def test_idempotent(self):
        self.assertEqual(
            _normalize_ollama_chat_url("http://localhost:11434/api/chat"),
            "http://localhost:11434/api/chat",
        )

    def test_api_root(self):
        self.assertEqual(
            _normalize_ollama_chat_url("http://localhost:11434/api"),
            "http://localhost:11434/api/chat",
        )

    def test_empty(self):
        self.assertEqual(_normalize_ollama_chat_url(""), "")


class _FakeResponse:
    status_code = 200

    def __init__(self, content="ok"):
        self._content = content
        self.text = ""

    def json(self):
        return {"message": {"content": self._content}}


class ContextWindowTests(unittest.TestCase):
    """Ollama silently truncates the FRONT of prompts past num_ctx (default
    ~4096) — losing the instruction block first. We must always request a
    larger window, and never let the conversation history grow unboundedly."""

    def _capture_request(self, coro):
        captured = {}

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None):
                captured["url"] = url
                captured["body"] = json
                return _FakeResponse()

        with patch("pipeline.ollama_translator.httpx.AsyncClient", lambda **kw: _FakeClient()):
            asyncio.run(coro)
        return captured

    def test_num_ctx_sent(self):
        captured = self._capture_request(
            ollama_chat("http://localhost:11434", "m", [{"role": "user", "content": "hi"}])
        )
        self.assertEqual(captured["body"]["options"]["num_ctx"], OLLAMA_NUM_CTX)
        self.assertGreaterEqual(OLLAMA_NUM_CTX, 8192)

    def test_send_text_is_stateless(self):
        # Each chunk prompt is self-contained; resending history would grow
        # every request until num_ctx truncation eats the instructions.
        tr = OllamaTrackTranslator(model="m", base_url="http://localhost:11434")

        async def _two_calls():
            await tr._send_text("first chunk prompt")
            await tr._send_text("second chunk prompt")

        captured = self._capture_request(_two_calls())
        messages = captured["body"]["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["content"], "second chunk prompt")


class RequestFormatSettingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls._old_db_path = database.DB_PATH
        database.DB_PATH = Path(cls._tmpdir.name) / "test.db"
        asyncio.run(database.init_db())

    @classmethod
    def tearDownClass(cls):
        database.DB_PATH = cls._old_db_path
        cls._tmpdir.cleanup()

    def test_ollama_accepted_and_round_trips(self):
        asyncio.run(database.set_runtime_openai_compat_request_format("ollama"))
        self.assertEqual(
            asyncio.run(database.get_runtime_openai_compat_request_format()), "ollama"
        )

    def test_invalid_rejected(self):
        with self.assertRaises(ValueError):
            asyncio.run(database.set_runtime_openai_compat_request_format("llamacpp"))


if __name__ == "__main__":
    unittest.main()
