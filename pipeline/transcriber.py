import asyncio
import atexit
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Optional

import torch
from config import FFMPEG_PATH

logger = logging.getLogger(__name__)

# Track temp files to clean up on exit
_temp_files = []


def _cleanup_temp_files():
    """Clean up any remaining temp files on exit."""
    for temp_file in _temp_files:
        try:
            if temp_file.exists():
                temp_file.unlink()
                logger.debug(f"Cleaned up temp file: {temp_file}")
        except Exception as e:
            logger.warning(f"Failed to clean up temp file {temp_file}: {e}")


atexit.register(_cleanup_temp_files)


class WhisperTranscriber:
    """
    Lazy-loaded Faster Whisper transcriber with GPU/CPU detection and chunk-level progress callbacks.
    Offloads blocking transcription to thread pool to avoid event loop blocking.
    Handles Unicode file paths by copying to temp location with ASCII-safe names.
    """

    _instance = None
    _model = None
    _device = None
    _compute_type = None

    def __init__(
        self,
        model_name: str = "small",
        progress_callback=None,
        vad_filter: bool = False,
        beam_size: int = 5,
        condition_on_previous_text: bool = True,
    ):
        self.model_name = model_name
        self._device, self._compute_type = self._detect_device()
        self.progress_callback = progress_callback  # Optional callback for progress updates
        self.vad_filter = bool(vad_filter)
        self.beam_size = int(beam_size) if beam_size else 5
        self.condition_on_previous_text = bool(condition_on_previous_text)

    @staticmethod
    def _detect_device() -> tuple[str, str]:
        """Detect CUDA availability, prefer GPU. Returns (device, compute_type)."""
        if torch.cuda.is_available():
            return ("cuda", "float16")  # GPU with FP16 for speed
        return ("cpu", "int8")  # CPU with int8 for memory efficiency

    def _load_model(self):
        """Lazy-load Faster Whisper model on first use."""
        if self._model is not None:
            return

        try:
            from faster_whisper import WhisperModel

            logger.info(f"Loading Faster Whisper model '{self.model_name}' on device '{self._device}' with compute_type '{self._compute_type}'")
            self._model = WhisperModel(self.model_name, device=self._device, compute_type=self._compute_type)
            logger.info(f"Faster Whisper model loaded successfully")
        except ImportError:
            raise RuntimeError("faster-whisper not installed. Install with: pip install faster-whisper")
        except Exception as e:
            raise RuntimeError(f"Failed to load Faster Whisper model '{self.model_name}': {e}")

    @staticmethod
    def _prepare_ffmpeg() -> None:
        if not FFMPEG_PATH:
            return
        ffmpeg_path = Path(FFMPEG_PATH)
        if not ffmpeg_path.is_file():
            raise RuntimeError(f"Configured DRAMACD_FFMPEG_PATH does not exist: {FFMPEG_PATH}")
        ffmpeg_dir = str(ffmpeg_path.parent)
        current_path = os.environ.get("PATH", "")
        parts = current_path.split(os.pathsep) if current_path else []
        if ffmpeg_dir not in parts:
            os.environ["PATH"] = ffmpeg_dir + (os.pathsep + current_path if current_path else "")

    def _transcribe_blocking(self, audio_path: Path) -> dict:
        """Blocking transcription (to be run in thread pool)."""
        self._prepare_ffmpeg()
        self._load_model()

        # Handle Unicode paths by copying to temp location with ASCII-safe filename
        work_path = audio_path
        temp_copy = None
        try:
            has_unicode = any(ord(c) > 127 for c in str(audio_path))
            logger.debug(f"Transcribing: {audio_path.name} (unicode={has_unicode}, exists={audio_path.exists()})")

            if has_unicode:
                # Path contains non-ASCII characters - copy to temp
                ext = audio_path.suffix.lower()
                with tempfile.NamedTemporaryFile(suffix=ext, delete=False, dir=None) as tmp:
                    temp_copy = Path(tmp.name)

                logger.debug(f"Copying to temp: {temp_copy}")
                shutil.copy2(audio_path, temp_copy)
                logger.debug(f"Copy successful, temp file size: {temp_copy.stat().st_size} bytes")

                work_path = temp_copy
                # Track for cleanup
                _temp_files.append(temp_copy)

            logger.debug(f"Transcribing from: {work_path}")

            # Transcribe with Faster Whisper (progress callback happens automatically in generator loop)
            result = self._transcribe_with_progress(str(work_path), language="ja")
            logger.debug(f"Transcription successful, got {len(result.get('segments', []))} segments")
            return result
        except Exception as e:
            logger.error(f"Transcription failed: {e}", exc_info=True)
            # If transcription fails, try to clean up temp file immediately
            if temp_copy and temp_copy.exists():
                try:
                    temp_copy.unlink()
                    if temp_copy in _temp_files:
                        _temp_files.remove(temp_copy)
                    logger.debug(f"Cleaned up failed temp file: {temp_copy}")
                except Exception as cleanup_err:
                    logger.warning(f"Failed to clean up temp file: {cleanup_err}")
            raise

    def _transcribe_with_progress(self, audio_path: str, language: str = "ja") -> dict:
        """
        Transcribe audio using Faster Whisper with real-time progress callbacks.

        Args:
            audio_path: Path to audio file
            language: Language code (e.g., 'ja', 'en')

        Returns:
            dict with 'segments' list in OpenAI Whisper format
        """
        import time

        logger.debug(f"[FASTER-WHISPER] Starting transcription: {audio_path}")
        self._load_model()

        start_time = time.time()
        segments = []
        segment_count = 0

        try:
            # Faster Whisper returns a generator of segments with real-time progress
            # transcribe() returns (segments_generator, info)
            segments_generator, info = self._model.transcribe(
                str(audio_path),
                language=language,
                beam_size=self.beam_size,
                word_timestamps=True,
                vad_filter=self.vad_filter,
                condition_on_previous_text=self.condition_on_previous_text,
            )

            logger.info(
                f"[FASTER-WHISPER] Audio duration: {info.duration:.1f}s, language: {info.language} "
                f"(prob: {info.language_probability:.2f}) | model={self.model_name} "
                f"beam={self.beam_size} vad={self.vad_filter} condition_prev={self.condition_on_previous_text}"
            )

            # Process segments as they arrive (this is where real-time callbacks happen)
            for segment in segments_generator:
                segment_count += 1
                segments.append({
                    "id": segment.id,
                    "start": segment.start,
                    "end": segment.end,
                    "text": segment.text
                })

                # Call progress callback if provided
                if self.progress_callback and segment_count % 5 == 0:  # Report every 5 segments
                    # Calculate approximate progress based on time elapsed vs total duration
                    progress_percent = int((segment.end / info.duration) * 100) if info.duration > 0 else 0
                    self.progress_callback(min(95, progress_percent))  # Cap at 95% until done

                logger.debug(f"[FASTER-WHISPER] Segment {segment_count}: {segment.start:.1f}s-{segment.end:.1f}s: {segment.text[:50]}")

            elapsed = time.time() - start_time
            logger.info(f"[FASTER-WHISPER] Transcription complete in {elapsed:.1f}s: {segment_count} segments")

            # Call final progress callback
            if self.progress_callback:
                self.progress_callback(100)

            # Return in OpenAI Whisper format for compatibility
            return {
                "text": " ".join(s["text"] for s in segments),
                "segments": segments,
                "language": info.language
            }

        except Exception as e:
            logger.error(f"[FASTER-WHISPER] Transcription failed: {e}")
            raise

    async def transcribe(self, audio_path: Path) -> dict:
        """
        Async wrapper for transcription.
        Offloads heavy work to thread pool to avoid blocking event loop.
        """
        return await asyncio.to_thread(self._transcribe_blocking, audio_path)

    def get_device(self) -> str:
        """Get current device (cuda or cpu)."""
        return self._device

    def get_model_name(self) -> str:
        """Get loaded model name."""
        return self.model_name

    @staticmethod
    def _normalize_text(text: str) -> str:
        """
        Normalize text for deduplication comparison.
        Removes punctuation and extra whitespace to catch near-duplicates.
        """
        # Remove punctuation and normalize whitespace
        normalized = re.sub(r'[^\w\s\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]', '', text)
        normalized = re.sub(r'\s+', ' ', normalized).strip().lower()
        return normalized

    @staticmethod
    def deduplicate_segments(segments: list[dict]) -> list[dict]:
        """
        Remove Whisper hallucination artifacts (consecutive identical/near-identical segments)
        while preserving actual repetitions that appear non-consecutively.

        Strategy:
        1. Remove consecutive exact duplicates
        2. Remove consecutive near-duplicates (same text after normalization)
        3. Keep segments that repeat but are not adjacent (intentional repetition)
        4. Preserve all segment indices and timing

        Example:
        Input:  ["おはよう", "おはよう", "おはよう", "こんにちは", "おはよう"]
        Output: ["おはよう", "こんにちは", "おはよう"]  <- only removes consecutive copies
        """
        if not segments:
            return segments

        deduplicated = []
        for i, current_seg in enumerate(segments):
            current_text = str(current_seg.get("text", "")).strip()

            # Get previous segment if it exists
            if deduplicated:
                prev_seg = deduplicated[-1]
                prev_text = str(prev_seg.get("text", "")).strip()

                # Check for exact match
                if current_text == prev_text:
                    logger.debug(
                        f"[deduplicate_segments] Removing consecutive duplicate at index {i}: "
                        f"'{current_text[:50]}...'"
                    )
                    continue

                # Check for near-duplicate (normalized text match)
                if (
                    current_text
                    and prev_text
                    and WhisperTranscriber._normalize_text(current_text)
                    == WhisperTranscriber._normalize_text(prev_text)
                ):
                    logger.debug(
                        f"[deduplicate_segments] Removing consecutive near-duplicate at index {i}: "
                        f"'{current_text[:50]}' (prev: '{prev_text[:50]}')"
                    )
                    continue

            deduplicated.append(current_seg)

        if len(deduplicated) < len(segments):
            logger.info(
                f"[deduplicate_segments] Removed {len(segments) - len(deduplicated)} hallucinated segments "
                f"({len(segments)} → {len(deduplicated)})"
            )

        return deduplicated

    @staticmethod
    def parse_segments(whisper_output: dict, deduplicate: bool = True) -> list[dict]:
        """
        Parse Whisper output into segment format for database.
        Optionally deduplicates consecutive hallucinated segments.
        """
        segments = []
        for seg in whisper_output.get("segments", []):
            segments.append(
                {
                    "segment_index": seg.get("id", 0),  # Whisper’s raw id (temporary)
                    "start_seconds": seg.get("start", 0.0),
                    "end_seconds": seg.get("end", 0.0),
                    "text": seg.get("text", "").strip(),
                    "confidence": None,
                    "meta": None,
                }
            )
        # Step 1: dedupe hallucinated repeats
        if deduplicate:
            segments = WhisperTranscriber.deduplicate_segments(segments)

        # ⭐ Step 2: FIX — renumber sequentially after dedupe
        for new_index, seg in enumerate(segments):
            seg["segment_index"] = new_index

        return segments
