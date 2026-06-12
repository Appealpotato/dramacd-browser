"""update_item_user_data — clearing nullable text fields with an empty
string must store NULL (the PUT endpoint drops null fields via exclude_none,
so '' is the only way the UI can express "clear this")."""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database


class ClearFieldsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls._old_db_path = database.DB_PATH
        database.DB_PATH = Path(cls._tmpdir.name) / "test.db"
        asyncio.run(database.init_db())
        asyncio.run(database.upsert_item({
            "product_code": "RJ000001",
            "original_code": None,
            "confidence": "verified",
            "files": json.dumps([]),
            "file_count": 0,
            "total_size": 0,
            "file_format": json.dumps([]),
        }))
        cls.item_id = asyncio.run(database.get_item_by_product_code("RJ000001"))["id"]

    @classmethod
    def tearDownClass(cls):
        database.DB_PATH = cls._old_db_path
        cls._tmpdir.cleanup()

    def test_empty_string_clears_translated_fields(self):
        asyncio.run(database.update_item_user_data(self.item_id, {
            "title_en": "Some Title",
            "description_en": "Some description",
        }))
        item = asyncio.run(database.get_item(self.item_id))
        self.assertEqual(item["title_en"], "Some Title")

        asyncio.run(database.update_item_user_data(self.item_id, {
            "title_en": "",
            "description_en": "   ",
        }))
        item = asyncio.run(database.get_item(self.item_id))
        self.assertIsNone(item["title_en"])
        self.assertIsNone(item["description_en"])

    def test_blank_title_is_ignored_not_cleared(self):
        asyncio.run(database.update_item_user_data(self.item_id, {"title": "Real Title"}))
        asyncio.run(database.update_item_user_data(self.item_id, {"title": "  ", "notes": "kept"}))
        item = asyncio.run(database.get_item(self.item_id))
        self.assertEqual(item["title"], "Real Title")
        self.assertEqual(item["notes"], "kept")


if __name__ == "__main__":
    unittest.main()
