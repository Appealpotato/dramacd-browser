#!/usr/bin/env python3
"""
Test if Whisper actually outputs verbose progress to stderr.
This helps us understand why the progress bar isn't updating.
"""

import sys
import io
import logging

# Set up logging
logging.basicConfig(level=logging.DEBUG, format='[%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

try:
    import whisper
    logger.info("Whisper imported successfully")
except ImportError:
    logger.error("Whisper not installed. Install with: pip install openai-whisper")
    sys.exit(1)

# Check if we have a test audio file
from pathlib import Path
test_audio = Path("data/pipeline/test_audio.wav")

if not test_audio.exists():
    logger.error(f"No test audio found at {test_audio}")
    logger.info("To test, place a short WAV file at: data/pipeline/test_audio.wav")
    sys.exit(1)

logger.info(f"Testing Whisper verbose output with: {test_audio}")
logger.info(f"File size: {test_audio.stat().st_size} bytes")

# Test 1: Capture stderr
logger.info("\n" + "="*60)
logger.info("TEST 1: Capturing stderr during transcription")
logger.info("="*60)

old_stderr = sys.stderr
captured = io.StringIO()
sys.stderr = captured

try:
    logger.info("Loading model (this may take a moment)...")
    model = whisper.load_model("tiny", device="cpu")  # Use tiny for fast test
    logger.info("Running transcription with verbose=1...")
    result = model.transcribe(str(test_audio), verbose=1, language="ja")
finally:
    sys.stderr = old_stderr
    stderr_output = captured.getvalue()

logger.info(f"Transcription complete")
logger.info(f"Captured {len(stderr_output)} bytes from stderr")

if stderr_output:
    logger.info("\nFirst 500 characters of stderr output:")
    logger.info(stderr_output[:500])

    # Count progress lines
    lines = stderr_output.split('\n')
    progress_lines = [l for l in lines if '%|' in l]
    logger.info(f"\nFound {len(progress_lines)} progress lines (containing '%|')")

    if progress_lines:
        logger.info("Sample progress lines:")
        for line in progress_lines[:5]:
            logger.info(f"  {line}")
    else:
        logger.error("No progress lines found! Verbose output might not be working.")
        logger.info("\nAll lines containing %:")
        percent_lines = [l for l in lines if '%' in l]
        for line in percent_lines[:5]:
            logger.info(f"  {line}")
else:
    logger.error("stderr captured nothing!")
    logger.info("This means Whisper is not outputting to stderr when verbose=1")
    logger.info("The progress parsing approach won't work.")

# Test 2: Try with verbose=2
logger.info("\n" + "="*60)
logger.info("TEST 2: Trying with verbose=2")
logger.info("="*60)

old_stderr = sys.stderr
captured2 = io.StringIO()
sys.stderr = captured2

try:
    logger.info("Running transcription with verbose=2...")
    result2 = model.transcribe(str(test_audio), verbose=2, language="ja")
finally:
    sys.stderr = old_stderr
    stderr_output2 = captured2.getvalue()

logger.info(f"Captured {len(stderr_output2)} bytes from stderr with verbose=2")

if stderr_output2:
    progress_lines2 = [l for l in stderr_output2.split('\n') if '%|' in l or '%' in l]
    logger.info(f"Found {len(progress_lines2)} lines with % symbol")
else:
    logger.error("verbose=2 also produced no output!")

print("\n" + "="*60)
print("SUMMARY")
print("="*60)
if progress_lines:
    print("✓ Whisper DOES output progress to stderr with verbose=1")
    print(f"  → Progress parsing should work")
elif progress_lines2:
    print("⚠ Whisper outputs progress with verbose=2, not verbose=1")
    print(f"  → Need to change transcriber.py to use verbose=2")
else:
    print("✗ Whisper is NOT outputting progress to stderr")
    print(f"  → Progress parsing approach won't work")
    print(f"  → Need fallback to time-based or track-based progress")
