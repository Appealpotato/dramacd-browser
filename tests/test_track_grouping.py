"""Track grouping (get_pipeline_track_groups) — regression for the
variant-aware duration gate.

Real-world shape from RJ01190059: the voice-only mix trims SFX-only
segments, so its tracks run 2-4s shorter than the SFX flac/mp3 versions.
With a flat ±2s duration gate those split into separate groups; the gate
must be loose (±30s) across DIFFERENT variants while staying strict (±2s)
within the SAME variant so freetalk/bonus tracks sharing a stem prefix
still split."""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database


def _track(tid, path, codec, duration):
    return {
        "id": tid,
        "track_index": tid,
        "track_path": path,
        "codec": codec,
        "duration_seconds": duration,
        "transcript_run_count": 0,
        "translation_run_count": 0,
    }


# Mirrors RJ01190059's extracted layout (durations are the real probe values).
RJ01190059_TRACKS = [
    # 1.本編_flac (sfx)
    _track(1, r"X:\ws\RJ01190059\1.本編_flac\track1.flac", "flac", 306.663792),
    _track(2, r"X:\ws\RJ01190059\1.本編_flac\track2.flac", "flac", 1051.0),
    _track(3, r"X:\ws\RJ01190059\1.本編_flac\track6.flac", "flac", 1485.514292),
    # 2.本編_mp3 (sfx; note track6 carries a _casttalk suffix here)
    _track(4, r"X:\ws\RJ01190059\2.本編_mp3\track1.mp3", "mp3", 306.663792),
    _track(5, r"X:\ws\RJ01190059\2.本編_mp3\track2.mp3", "mp3", 1051.0),
    _track(6, r"X:\ws\RJ01190059\2.本編_mp3\track6_casttalk.mp3", "mp3", 1485.514292),
    # 3.voiceonly (no-sfx) — runs 2-4s SHORT of the sfx mix
    _track(7, r"X:\ws\RJ01190059\3.voiceonly\track1.mp3", "mp3", 304.649729),
    _track(8, r"X:\ws\RJ01190059\3.voiceonly\track2.mp3", "mp3", 1047.874146),
    _track(9, r"X:\ws\RJ01190059\3.voiceonly\track6_casttalk.mp3", "mp3", 1483.276438),
    # Omake whose stem is ALSO "track1" but is a different recording — the
    # same-variant ±2s gate must keep it out of the main track1 group.
    _track(10, r"X:\ws\RJ01190059\4.おまけ\NATSU\track1.mp3", "mp3", 1672.493333),
]


def _groups(tracks, preferred_variant="sfx"):
    with patch.object(database, "get_pipeline_tracks", AsyncMock(return_value=tracks)):
        return asyncio.run(database.get_pipeline_track_groups(1, preferred_variant))


class VariantAwareDurationGateTests(unittest.TestCase):
    def test_voiceonly_variant_groups_with_sfx_despite_duration_skew(self):
        groups = _groups([dict(t) for t in RJ01190059_TRACKS])
        # track1(flac+mp3+voiceonly), track2(×3), track6(×3), NATSU track1
        self.assertEqual(len(groups), 4)
        by_size = sorted(groups, key=lambda g: -len(g["tracks"]))
        for g in by_size[:3]:
            self.assertEqual(len(g["tracks"]), 3)
            self.assertEqual(g["variants"], ["sfx", "no-sfx"])
        # The omake sharing the "track1" stem stays its own group.
        natsu = [g for g in groups if len(g["tracks"]) == 1]
        self.assertEqual(len(natsu), 1)
        self.assertIn("NATSU", natsu[0]["tracks"][0]["track_path"])

    def test_same_variant_far_durations_still_split(self):
        tracks = [
            _track(1, r"X:\ws\RJ\main\track1.flac", "flac", 300.0),
            _track(2, r"X:\ws\RJ\bonus\track1_freetalk.mp3", "mp3", 900.0),
        ]
        groups = _groups(tracks)
        self.assertEqual(len(groups), 2)

    def test_preferred_track_follows_variant_setting(self):
        tracks = [
            _track(1, r"X:\ws\RJ\main\track1.flac", "flac", 306.66),
            _track(2, r"X:\ws\RJ\voiceonly\track1.mp3", "mp3", 304.65),
        ]
        sfx_first = _groups([dict(t) for t in tracks], "sfx")
        self.assertEqual(len(sfx_first), 1)
        self.assertEqual(sfx_first[0]["preferred_track_id"], 1)
        nosfx_first = _groups([dict(t) for t in tracks], "no-sfx")
        self.assertEqual(nosfx_first[0]["preferred_track_id"], 2)


if __name__ == "__main__":
    unittest.main()
