#!/usr/bin/env python3
"""
Test track-based progress calculation.
Shows how progress should increase as tracks complete.
"""
import sys
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')

def calculate_progress(completed, total, current_track_percent=0):
    """Calculate overall progress based on completed tracks."""
    if total == 0:
        return 0

    base_progress = (completed / total) * 100
    current_contribution = (current_track_percent / 100) * (100 / total)
    overall_progress = int(base_progress + current_contribution)
    return overall_progress

# Test scenario: 3 tracks
print("=" * 60)
print("Progress for 3-track transcription")
print("=" * 60)

total_tracks = 3
scenarios = [
    (0, 0, "Starting"),
    (0, 25, "Processing track 1 (25%)"),
    (0, 50, "Processing track 1 (50%)"),
    (0, 100, "Processing track 1 (100%)"),
    (1, 0, "Completed track 1, starting track 2"),
    (1, 25, "Track 2 (25%)"),
    (1, 50, "Track 2 (50%)"),
    (1, 100, "Track 2 (100%)"),
    (2, 0, "Completed track 2, starting track 3"),
    (2, 50, "Track 3 (50%)"),
    (2, 100, "Track 3 (100%)"),
    (3, 0, "All complete"),
]

print(f"\nWith {total_tracks} tracks:\n")
for completed, within_track, desc in scenarios:
    prog = calculate_progress(completed, total_tracks, within_track)
    bar = "█" * (prog // 5) + "░" * (20 - prog // 5)
    print(f"{prog:3d}% [{bar}] {desc}")

# Test with 1 track
print("\n" + "=" * 60)
print("Progress for 1-track transcription")
print("=" * 60)

total_tracks = 1
scenarios_1 = [
    (0, 0, "Starting"),
    (0, 25, "Processing (25%)"),
    (0, 50, "Processing (50%)"),
    (0, 75, "Processing (75%)"),
    (0, 100, "Processing (100%)"),
    (1, 0, "Complete"),
]

print(f"\nWith {total_tracks} track:\n")
for completed, within_track, desc in scenarios_1:
    prog = calculate_progress(completed, total_tracks, within_track)
    bar = "█" * (prog // 5) + "░" * (20 - prog // 5)
    print(f"{prog:3d}% [{bar}] {desc}")

# Test with 10 tracks
print("\n" + "=" * 60)
print("Progress for 10-track transcription (sample)")
print("=" * 60)

total_tracks = 10
print(f"\nWith {total_tracks} tracks (each track adds {100/total_tracks:.0f}%):\n")
for i in range(0, 11):
    prog = calculate_progress(i, total_tracks, 0)
    bar = "█" * (prog // 5) + "░" * (20 - prog // 5)
    print(f"{prog:3d}% [{bar}] Completed {i} / {total_tracks} tracks")

print("\n" + "=" * 60)
print("Key Insights")
print("=" * 60)
print(f"""
1. With N tracks, each completed track adds (100/N)% to progress
2. With 3 tracks: +33.3% per track
3. With 10 tracks: +10% per track
4. Within-track progress provides smooth interpolation
5. Final progress always reaches 100% when done

This approach:
✓ Works without Whisper's internal progress
✓ Smooth visual progress bar
✓ Accurate tracking
✓ Simple math, no parsing needed
""")
