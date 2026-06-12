"""Ollama request format — URL normalization and settings validation."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database
from pipeline.ollama_translator import _normalize_ollama_chat_url


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
