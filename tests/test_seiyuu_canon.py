"""Canon seiyuu romanization: pinning JP→EN spellings and enforcing them
over fresh LLM translation output."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database


class SeiyuuCanonTests(unittest.TestCase):
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

    def test_pin_round_trip(self):
        asyncio.run(database.pin_seiyuu_romanization("茶介", "Chasuke"))

        async def read():
            db = await database.get_db()
            try:
                cur = await db.execute(
                    "SELECT alias, canonical_en, canonical_jp FROM seiyuu_aliases WHERE canonical_jp = '茶介'"
                )
                return [dict(r) for r in await cur.fetchall()]
            finally:
                await db.close()

        rows = asyncio.run(read())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["canonical_en"], "Chasuke")
        self.assertEqual(rows[0]["alias"], "Chasuke")  # self-alias carries the pin

    def test_pin_overwrites_previous(self):
        asyncio.run(database.pin_seiyuu_romanization("三楽章", "Sangakushou"))
        asyncio.run(database.pin_seiyuu_romanization("三楽章", "San Gakushou"))
        out, replaced = asyncio.run(
            database.normalize_translated_seiyuu(["三楽章"], ["Sangaku-sho"])
        )
        self.assertEqual(out, ["San Gakushou"])
        self.assertEqual(replaced, 1)

    def test_pin_rejects_blank(self):
        with self.assertRaises(ValueError):
            asyncio.run(database.pin_seiyuu_romanization("", "X"))
        with self.assertRaises(ValueError):
            asyncio.run(database.pin_seiyuu_romanization("茶介", "  "))

    def test_normalize_forces_pin_over_model_spelling(self):
        asyncio.run(database.pin_seiyuu_romanization("茶介", "Chasuke"))
        out, replaced = asyncio.run(
            database.normalize_translated_seiyuu(
                ["茶介", "佐和真中"], ["Teasuke", "Manaka Sawa"]
            )
        )
        self.assertEqual(out[0], "Chasuke")
        self.assertEqual(out[1], "Manaka Sawa")  # unknown name untouched
        self.assertEqual(replaced, 1)

    def test_normalize_maps_alias_spellings(self):
        asyncio.run(
            database.merge_seiyuu_aliases(
                "Kawamura Spica", ["Kawamura Supika"], canonical_jp="河村スピカ", dry_run=False
            )
        )
        out, replaced = asyncio.run(
            database.normalize_translated_seiyuu([], ["Kawamura Supika"])
        )
        self.assertEqual(out, ["Kawamura Spica"])
        self.assertEqual(replaced, 1)

    def test_normalize_misaligned_lists_skip_positional(self):
        asyncio.run(database.pin_seiyuu_romanization("茶介", "Chasuke"))
        # JP list shorter than EN list — positional enforcement must not fire.
        out, replaced = asyncio.run(
            database.normalize_translated_seiyuu(["茶介"], ["Some Guy", "Another Guy"])
        )
        self.assertEqual(out, ["Some Guy", "Another Guy"])
        self.assertEqual(replaced, 0)

    def test_normalize_empty(self):
        out, replaced = asyncio.run(database.normalize_translated_seiyuu([], []))
        self.assertEqual(out, [])
        self.assertEqual(replaced, 0)


if __name__ == "__main__":
    unittest.main()
