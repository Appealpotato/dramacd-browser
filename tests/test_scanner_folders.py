"""Codeless-collection scanning: folder imports, loose-archive imports,
title cleaning, and the run_scan ingestion pass (temp DB + temp tree)."""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aiosqlite

import database
from scanner import clean_title, scan_folder_with_progress


def build_tree(root: Path):
    """A miniature of the friend's library layout."""
    (root / "CRAZY CIRCUS").mkdir()
    (root / "CRAZY CIRCUS" / "Vol1.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
    (root / "CRAZY CIRCUS" / "Vol2.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
    (root / "CRAZY CIRCUS" / "cover.jpg").write_bytes(b"\xff\xd8junk")  # not scannable

    (root / "Dear Vocalist").mkdir()
    (root / "Dear Vocalist" / "Track 1.mp3").write_bytes(b"ID3junk")

    nested = root / "帝國スタア" / "Disc 1"
    nested.mkdir(parents=True)
    (nested / "Vol1.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)

    # mixed folder: holds a coded archive → not a folder import
    (root / "Mixed").mkdir()
    (root / "Mixed" / "RJ222222 something.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
    (root / "Mixed" / "stray.mp3").write_bytes(b"ID3junk")

    # coded folder name → not a folder import (normal flow owns it)
    (root / "RJ333333 series").mkdir()
    (root / "RJ333333 series" / "noname.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)

    # top-level coded archive → normal item
    (root / "RJ123456.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)

    # top-level codeless archives: multi-part set + single
    (root / "LooseSeries.part1.rar").write_bytes(b"Rar!junk1")
    (root / "LooseSeries.part2.rar").write_bytes(b"Rar!junk2")
    (root / "週刊添い寝CDシリーズ_[MP3].zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)


class CleanTitleTests(unittest.TestCase):
    def test_noise_stripping(self):
        self.assertEqual(
            clean_title("Blood-Stained Flowers Paid Remake MP4 [RAW] (DLsite)"),
            "Blood-Stained Flowers Paid Remake MP4",
        )
        self.assertEqual(clean_title("週刊添い寝CDシリーズ_[MP3]"), "週刊添い寝CDシリーズ")
        self.assertEqual(clean_title("some_name_with_underscores"), "some name with underscores")

    def test_meaningful_symbols_survive(self):
        self.assertEqual(clean_title("√HAPPY SUGAR"), "√HAPPY SUGAR")
        self.assertEqual(clean_title("MOTTO♥LIP ON MY PRINCE"), "MOTTO♥LIP ON MY PRINCE")
        self.assertEqual(clean_title("LOVE★DON!!★QUIXOTE"), "LOVE★DON!!★QUIXOTE")


class FolderScanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        build_tree(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_scan_collects_folder_imports(self):
        result = scan_folder_with_progress(scan_paths=[str(self.root)], recursive=True)
        self.assertIn("RJ123456", result["items"])
        self.assertIn("RJ222222", result["items"])
        self.assertIn("RJ333333", result["items"])

        imports = {fi["title"]: fi for fi in result["folder_imports"]}
        self.assertIn("CRAZY CIRCUS", imports)
        self.assertIn("Dear Vocalist", imports)
        self.assertIn("帝國スタア", imports)
        self.assertNotIn("Mixed", imports)            # holds a coded member
        self.assertNotIn("RJ333333 series", imports)  # coded folder name
        # nested archives collected as absolute paths
        cc = imports["CRAZY CIRCUS"]
        self.assertEqual(cc["file_count"], 2)
        self.assertTrue(all(Path(f).is_absolute() for f in cc["files"]))
        self.assertEqual(len(imports["帝國スタア"]["files"]), 1)
        self.assertEqual(result["stats"]["folder_imports"], 3)

    def test_non_recursive_scan_skips_folder_imports(self):
        result = scan_folder_with_progress(scan_paths=[str(self.root)], recursive=False)
        self.assertEqual(result["folder_imports"], [])


class RunScanIngestionTests(unittest.TestCase):
    """Full run_scan against a temp DB: folder + loose-archive entries land
    as manual items; rescans don't duplicate."""

    @classmethod
    def setUpClass(cls):
        cls._tmpdb = tempfile.TemporaryDirectory()
        cls._old_db_path = database.DB_PATH
        database.DB_PATH = Path(cls._tmpdb.name) / "test.db"
        asyncio.run(database.init_db())

    @classmethod
    def tearDownClass(cls):
        database.DB_PATH = cls._old_db_path
        cls._tmpdb.cleanup()

    def test_run_scan_creates_manual_entries_idempotently(self):
        from routers.scan import run_scan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_tree(root)
            asyncio.run(run_scan(scan_paths=[str(root)], recursive=True))

            async def _rows():
                async with aiosqlite.connect(database.DB_PATH) as conn:
                    conn.row_factory = aiosqlite.Row
                    cur = await conn.execute(
                        "SELECT product_code, title, is_manual, files, file_count "
                        "FROM items ORDER BY product_code"
                    )
                    return [dict(r) for r in await cur.fetchall()]

            rows = _r1 = asyncio.run(_rows())
            by_title = {r["title"]: r for r in rows if r["title"]}
            # folder imports present, manual, cleaned titles
            self.assertIn("CRAZY CIRCUS", by_title)
            self.assertIn("Dear Vocalist", by_title)
            self.assertIn("帝國スタア", by_title)
            self.assertTrue(by_title["CRAZY CIRCUS"]["is_manual"])
            self.assertTrue(by_title["CRAZY CIRCUS"]["product_code"].startswith("MAN-"))
            self.assertEqual(by_title["CRAZY CIRCUS"]["file_count"], 2)
            files = json.loads(by_title["CRAZY CIRCUS"]["files"])
            self.assertTrue(all(Path(f).is_absolute() for f in files))
            # loose archives: noise stripped, multipart grouped into ONE entry
            self.assertIn("週刊添い寝CDシリーズ", by_title)
            self.assertIn("LooseSeries", by_title)
            loose_files = json.loads(by_title["LooseSeries"]["files"])
            self.assertEqual(len(loose_files), 2)  # both parts merged
            # coded items intact
            codes = {r["product_code"] for r in rows}
            self.assertIn("RJ123456", codes)
            self.assertIn("RJ222222", codes)

            # rescan: no duplicates
            asyncio.run(run_scan(scan_paths=[str(root)], recursive=True))
            rows2 = asyncio.run(_rows())
            self.assertEqual(len(rows2), len(_r1))

            # nothing claimed by imports leaks into unmatched except 'stray.mp3'
            async def _unmatched():
                async with aiosqlite.connect(database.DB_PATH) as conn:
                    conn.row_factory = aiosqlite.Row
                    cur = await conn.execute("SELECT filename FROM unmatched_files")
                    return sorted(r["filename"] for r in await cur.fetchall())

            unmatched = asyncio.run(_unmatched())
            self.assertEqual(unmatched, ["stray.mp3"])


class ClaimedFolderTests(unittest.TestCase):
    """Regression: a folder whose archive is already owned by an existing
    entry (archive_path / Browse flow) must NOT import as a duplicate —
    but a folder-per-CD layout still imports fresh folders, and rescans of
    a folder-imported entry keep working (self-ownership exception)."""

    def setUp(self):
        self._tmpdb = tempfile.TemporaryDirectory()
        self._old_db_path = database.DB_PATH
        database.DB_PATH = Path(self._tmpdb.name) / "test.db"
        asyncio.run(database.init_db())

    def tearDown(self):
        database.DB_PATH = self._old_db_path
        self._tmpdb.cleanup()

    def test_owned_folder_member_blocks_import_but_rescan_self_ok(self):
        from routers.scan import run_scan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            owned_dir = root / "Seventh Heaven Vol.1"
            owned_dir.mkdir()
            owned_archive = owned_dir / "Seventh Heaven Vol.1.7z"
            owned_archive.write_bytes(b"7zjunk")
            fresh_dir = root / "CRAZY CIRCUS"
            fresh_dir.mkdir()
            (fresh_dir / "Vol1.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)

            async def _seed():
                async with aiosqlite.connect(database.DB_PATH) as conn:
                    await conn.execute(
                        """INSERT INTO items (product_code, title, kind, is_manual, files,
                                              file_count, created_at, updated_at)
                           VALUES ('MAN-EXISTING1', 'SEVENTH HEAVEN', 'drama_cd', 1, ?, 1,
                                   '2026-01-01', '2026-01-01')""",
                        (json.dumps([str(owned_archive)]),),
                    )
                    await conn.commit()

            asyncio.run(_seed())
            asyncio.run(run_scan(scan_paths=[str(root)], recursive=True))

            async def _rows():
                async with aiosqlite.connect(database.DB_PATH) as conn:
                    conn.row_factory = aiosqlite.Row
                    cur = await conn.execute("SELECT product_code, title FROM items")
                    return [dict(r) for r in await cur.fetchall()]

            rows = asyncio.run(_rows())
            titles = sorted(r["title"] for r in rows if r["title"])
            # no duplicate of the owned CD; the fresh folder still imports
            self.assertEqual(titles, ["CRAZY CIRCUS", "SEVENTH HEAVEN"])

            # rescan: the folder-imported entry (self-owned files) must not
            # get blocked by its own claim, and still no duplicates
            asyncio.run(run_scan(scan_paths=[str(root)], recursive=True))
            rows2 = asyncio.run(_rows())
            self.assertEqual(len(rows2), 2)


class TokutenOwnedTests(unittest.TestCase):
    """Regression (the ORTANM.7z case): files owned by TOKUTENS — via
    tokutens.local_path stubs or dict-shaped items.files from the tokuten
    folder-scan — must not re-import as manual drama CD entries."""

    def setUp(self):
        self._tmpdb = tempfile.TemporaryDirectory()
        self._old_db_path = database.DB_PATH
        database.DB_PATH = Path(self._tmpdb.name) / "test.db"
        asyncio.run(database.init_db())

    def tearDown(self):
        database.DB_PATH = self._old_db_path
        self._tmpdb.cleanup()

    def test_tokuten_owned_archive_and_folder_not_reimported(self):
        from routers.scan import run_scan

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # stub tokuten: top-level archive registered via tokuten path scan
            stub_archive = root / "ORTANM.7z"
            stub_archive.write_bytes(b"7zjunk")
            # folder tokuten: registered via folder-scan-once (dict-shaped files)
            tk_folder = root / "Bonus Disc"
            tk_folder.mkdir()
            tk_audio = tk_folder / "track01.mp3"
            tk_audio.write_bytes(b"ID3junk")

            async def _seed():
                now = "2026-01-01T00:00:00Z"
                async with aiosqlite.connect(database.DB_PATH) as conn:
                    await conn.execute(
                        """INSERT INTO tokutens (kind, title, shop, local_path,
                                                 created_at, updated_at)
                           VALUES ('audio', 'ORTANM', 'other', ?, ?, ?)""",
                        (str(stub_archive), now, now),
                    )
                    cur = await conn.execute(
                        """INSERT INTO tokutens (kind, title, shop, local_path,
                                                 created_at, updated_at)
                           VALUES ('audio', 'Bonus Disc', 'other', ?, ?, ?)""",
                        (str(tk_folder), now, now),
                    )
                    tokuten_id = cur.lastrowid
                    # paired item with the tokuten folder-scan's dict file shape
                    await conn.execute(
                        """INSERT INTO items (product_code, title, kind, tokuten_id,
                                              is_manual, files, file_count,
                                              created_at, updated_at)
                           VALUES ('TKT-OWNED00001', 'Bonus Disc', 'tokuten_audio', ?,
                                   1, ?, 1, ?, ?)""",
                        (tokuten_id,
                         json.dumps([{"filename": tk_audio.name, "path": str(tk_audio), "size": 7}]),
                         now, now),
                    )
                    await conn.commit()

            asyncio.run(_seed())
            asyncio.run(run_scan(scan_paths=[str(root)], recursive=True))

            async def _rows():
                async with aiosqlite.connect(database.DB_PATH) as conn:
                    conn.row_factory = aiosqlite.Row
                    cur = await conn.execute(
                        "SELECT product_code, title FROM items WHERE product_code LIKE 'MAN-%'"
                    )
                    return [dict(r) for r in await cur.fetchall()]

            dupes = asyncio.run(_rows())
            self.assertEqual(dupes, [], f"tokuten-owned files re-imported: {dupes}")


if __name__ == "__main__":
    unittest.main()
