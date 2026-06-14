"""Deleting a transcript/translation run must also remove the replicated copies
on sibling tracks, so the group's run-count badges actually clear (the bug was
the TL icon staying lit after delete because a FLAC/MP3 sibling kept its copy)."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database as db


def _run(coro):
    return asyncio.run(coro)


class DeleteRunSiblingsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = db.DB_PATH
        db.DB_PATH = Path(self._tmp.name) / "t.db"
        _run(db.init_db())
        self.item_id = _run(db.upsert_item({"product_code": "RJTEST_DELSIB"}))
        # Two siblings: same recording (stem "track01"), different codec.
        _run(db.replace_pipeline_tracks_for_item(self.item_id, [
            {"track_path": "/x/track01.flac", "codec": "flac", "duration_seconds": 100.0, "track_index": 1},
            {"track_path": "/x/track01.mp3", "codec": "mp3", "duration_seconds": 100.0, "track_index": 1},
        ]))
        tracks = _run(db.get_pipeline_tracks(self.item_id))
        self.flac = next(t for t in tracks if t["codec"] == "flac")["id"]
        self.mp3 = next(t for t in tracks if t["codec"] == "mp3")["id"]

    def tearDown(self):
        db.DB_PATH = self._old
        self._tmp.cleanup()

    @staticmethod
    def _segs_t(n=3):
        return [{"segment_index": i, "start_seconds": i, "end_seconds": i + 1, "text": f"l{i}"} for i in range(n)]

    @staticmethod
    def _segs_tl(n=3):
        return [{"segment_index": i, "text": f"e{i}"} for i in range(n)]

    def _setup_runs(self):
        """Transcript + translation on BOTH siblings (what replication produces)."""
        tr_flac = _run(db.create_transcript_run(self.flac, "ja", "whisper", "fw", "small", None, self._segs_t()))
        tr_mp3 = _run(db.create_transcript_run(self.mp3, "ja", "whisper", "fw", "small", None, self._segs_t()))
        tl_flac = _run(db.create_translation_run(self.flac, tr_flac, "en", "gemini", None, None, None, self._segs_tl()))
        tl_mp3 = _run(db.create_translation_run(self.mp3, tr_mp3, "en", "gemini", None, None, None, self._segs_tl()))
        return tr_flac, tr_mp3, tl_flac, tl_mp3

    def _group(self):
        groups = _run(db.get_pipeline_track_groups(self.item_id))
        return groups

    def test_siblings_form_one_group(self):
        self._setup_runs()
        groups = self._group()
        self.assertEqual(len(groups), 1, "flac+mp3 of the same recording should be one group")
        self.assertEqual(groups[0]["transcript_run_count"], 2)
        self.assertEqual(groups[0]["translation_run_count"], 2)

    def test_delete_translation_clears_sibling_replica(self):
        _, _, tl_flac, _ = self._setup_runs()
        res = _run(db.delete_translation_run_and_replicas(self.flac, tl_flac))
        # both translation runs gone; transcripts untouched
        self.assertEqual(_run(db.list_translation_runs(self.flac)), [])
        self.assertEqual(_run(db.list_translation_runs(self.mp3)), [])
        g = self._group()[0]
        self.assertEqual(g["translation_run_count"], 0, "TL badge must clear across the group")
        self.assertEqual(g["transcript_run_count"], 2, "transcripts must be untouched")
        self.assertEqual(len(res["deleted_run_ids"]), 2)
        self.assertEqual(set(res["tracks"]), {self.flac, self.mp3})

    def test_delete_transcript_cascades_and_clears_siblings(self):
        tr_flac, _, _, _ = self._setup_runs()
        _run(db.delete_transcript_run_and_replicas(self.flac, tr_flac))
        g = self._group()[0]
        self.assertEqual(g["transcript_run_count"], 0, "both transcripts gone")
        self.assertEqual(g["translation_run_count"], 0, "dependent translations cascade away")

    def test_delete_only_matches_same_language(self):
        # A second-language translation on the group must survive deleting the EN one.
        tr_flac, tr_mp3, tl_flac_en, tl_mp3_en = self._setup_runs()
        zh_flac = _run(db.create_translation_run(self.flac, tr_flac, "zh", "gemini", None, None, None, self._segs_tl()))
        zh_mp3 = _run(db.create_translation_run(self.mp3, tr_mp3, "zh", "gemini", None, None, None, self._segs_tl()))
        _run(db.delete_translation_run_and_replicas(self.flac, tl_flac_en))
        # EN gone on both, ZH kept on both
        langs_flac = sorted(r["target_language"] for r in _run(db.list_translation_runs(self.flac)))
        langs_mp3 = sorted(r["target_language"] for r in _run(db.list_translation_runs(self.mp3)))
        self.assertEqual(langs_flac, ["zh"])
        self.assertEqual(langs_mp3, ["zh"])


if __name__ == "__main__":
    unittest.main()
