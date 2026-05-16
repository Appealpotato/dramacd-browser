#!/usr/bin/env python3
"""
Test script to demonstrate Whisper progress parsing.
Shows how the regex extracts real progress % from verbose output.
"""

import re
import sys

# Force UTF-8 output on Windows
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')

# Sample Whisper verbose output (this is what gets written to stderr)
# Using ASCII instead of box-drawing chars for Windows compatibility
SAMPLE_OUTPUT = """[00:15<00:45] 25%|#####               | 15/60
[00:20<00:40] 33%|########              | 20/60
[00:30<00:30] 50%|####################    | 30/60
[00:40<00:20] 67%|#########################   | 40/60
[00:50<00:10] 83%|############################| 50/60
[00:55<00:05] 92%|#########################    | 55/60
[01:00<00:00]100%|############################| 60/60
Detected language: Japanese
"""

# Regex pattern used in transcriber.py
PROGRESS_PATTERN = r'\[[\d:]+<[\d:]+\]\s+(\d+)%'

print("=" * 60)
print("Whisper Progress Parsing Test")
print("=" * 60)

print("\nSample Whisper verbose output:")
print(SAMPLE_OUTPUT)

print("\nExtracting progress % with regex:", repr(PROGRESS_PATTERN))
matches = re.findall(PROGRESS_PATTERN, SAMPLE_OUTPUT)

print(f"\nAll matches found: {matches}")
print(f"Final progress: {matches[-1]}% (last match)")

print("\n" + "=" * 60)
print("How it works in real transcription:")
print("=" * 60)

# Simulate streaming progress (like the parser thread does)
print("\nSimulating real-time progress updates:")
output_buffer = ""
for line in SAMPLE_OUTPUT.split('\n'):
    if not line.strip():
        continue

    output_buffer += line + "\n"

    # Parse current buffer (like the parser thread does every 100ms)
    current_matches = re.findall(PROGRESS_PATTERN, output_buffer)
    if current_matches:
        progress = int(current_matches[-1])
        track_marker = "█" * (progress // 10) + "░" * (10 - progress // 10)
        print(f"  [{track_marker}] {progress:3d}%")

print("\n✓ Progress parsing complete!")
print("\nIn the actual app:")
print("  1. Whisper writes these lines to stderr during transcription")
print("  2. Parser thread reads stderr every 100ms")
print("  3. Regex extracts latest progress %")
print("  4. progress_callback(progress) updates job.current")
print("  5. Frontend polls and displays real-time progress bar")
