import os
import secrets
from pathlib import Path
from dotenv import load_dotenv

# Load .env file if it exists
load_dotenv()

# Unique id generated once per server process. The frontend reads this via
# /api/status to tell a browser refresh (same boot id -> keep transient UI
# state like the dragged scan/fetch progress widget) apart from an app
# restart (new boot id -> reset that state back to defaults).
BOOT_ID = secrets.token_hex(8)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        n = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


# Base directory of the application
APP_DIR = Path(__file__).parent

# Where to scan for drama CD archives. Set DRAMACD_SCAN_PATH in your .env file.
SCAN_PATH = os.environ.get("DRAMACD_SCAN_PATH", "").strip()

# Server settings
# LAN-accessible by default — this is a personal local app, so other devices
# on your network (phone, tablet) can reach it out of the box. Set
# DRAMACD_BIND_ALL=0 (or DRAMACD_HOST=127.0.0.1) to restrict it to localhost.
HOST = os.environ.get("DRAMACD_HOST", "").strip() or (
    "0.0.0.0" if _env_bool("DRAMACD_BIND_ALL", default=True) else "127.0.0.1"
)
PORT = int(os.environ.get("DRAMACD_PORT", "8080"))
API_KEY = os.environ.get("DRAMACD_API_KEY", "").strip() or None
ENABLE_PIPELINE = _env_bool("DRAMACD_ENABLE_PIPELINE", default=False)

# Database
DB_PATH = APP_DIR / "data" / "library.db"
PIPELINE_WORK_DIR = APP_DIR / "data" / "pipeline"
PIPELINE_EXTRACT_DIR = PIPELINE_WORK_DIR / "extracted"

# Cover art cache
COVERS_DIR = APP_DIR / "data" / "covers"

# DLsite API settings
DLSITE_REQUEST_DELAY = 1.0  # seconds between requests
DLSITE_SITE_SECTIONS = ["maniax", "home", "girls", "comic", "books", "pro"]
DLSITE_PROXY_URL = os.environ.get("DRAMACD_DLSITE_PROXY", "").strip() or None  # e.g., "http://proxy.example.com:8080" or "socks5://127.0.0.1:1080"

# Wayback Machine fallback for delisted DLsite works (fires only when every
# DLsite lookup 404'd — never on rate limits / transient errors)
WAYBACK_FALLBACK = _env_bool("DRAMACD_WAYBACK_FALLBACK", default=True)
WAYBACK_DELAY = float(os.environ.get("DRAMACD_WAYBACK_DELAY", "1.0"))  # courtesy delay between archive.org calls

# Supported archive extensions
ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z", ".tar"}

# Loose audio files picked up by the scanner (no extraction needed)
AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma"}

# Whisper transcription settings
WHISPER_MODEL = os.environ.get("DRAMACD_WHISPER_MODEL", "small")
WHISPER_DEVICE = "cuda" if os.environ.get("DRAMACD_WHISPER_DEVICE", "auto") == "auto" else os.environ.get("DRAMACD_WHISPER_DEVICE", "cpu")
# Batched decoding (faster-whisper BatchedInferencePipeline): ~3x faster on
# long audio when VAD is enabled, but benchmarks on real drama-CD audio show
# it merges VAD chunks aggressively and can swallow whole dialogue passages
# (86s of speech collapsed into one segment in testing). OFF by default —
# opt in via env only for content where speed matters more than fidelity.
try:
    WHISPER_BATCH_SIZE = max(0, int(os.environ.get("DRAMACD_WHISPER_BATCH_SIZE", "0")))
except ValueError:
    WHISPER_BATCH_SIZE = 0
FFMPEG_PATH = os.environ.get("DRAMACD_FFMPEG_PATH", "").strip() or None

# Pipeline concurrency caps (defaults; overridable at runtime via Settings).
# These bound how many jobs hit a shared, serial resource at once:
#   - Whisper transcription is GPU-bound — a single GPU does one decode at a
#     time, so >1 concurrent job just thrashes VRAM. Default 1.
#   - LLM translation is bound by the provider. A local box (Ollama/LM Studio)
#     serves one request at a time, so 6 "parallel" jobs really queue inside
#     the server and each blocks for minutes. Default 3 — a sane middle ground;
#     drop to 1-2 for a local GPU, raise for a high-throughput cloud provider.
MAX_WHISPER_JOBS = _env_int("DRAMACD_MAX_WHISPER_JOBS", 1, 1, 8)
MAX_LLM_JOBS = _env_int("DRAMACD_MAX_LLM_JOBS", 3, 1, 16)

# Translation provider settings
GEMINI_API_KEY = os.environ.get("DRAMACD_GEMINI_API_KEY", "").strip() or None
GEMINI_MODEL = os.environ.get("DRAMACD_GEMINI_MODEL", "gemini-2.0-flash")
OPENROUTER_API_KEY = os.environ.get("DRAMACD_OPENROUTER_API_KEY", "").strip() or None
# Use Claude 3.5 Sonnet for OpenRouter (best at following JSON format instructions)
# Fallback to auto if env var not set
OPENROUTER_MODEL = os.environ.get("DRAMACD_OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet")
CHUTES_API_KEY = os.environ.get("DRAMACD_CHUTES_API_KEY", "").strip() or None
# DeepSeek-V3.1 is the latest, better at JSON format
CHUTES_MODEL = os.environ.get("DRAMACD_CHUTES_MODEL", "deepseek-ai/DeepSeek-V3.1")
# Generic OpenAI-compatible provider (any /v1/chat/completions endpoint)
OPENAI_COMPAT_API_KEY = os.environ.get("DRAMACD_OPENAI_COMPAT_API_KEY", "").strip() or None
OPENAI_COMPAT_BASE_URL = os.environ.get("DRAMACD_OPENAI_COMPAT_BASE_URL", "").strip() or None
OPENAI_COMPAT_MODEL = os.environ.get("DRAMACD_OPENAI_COMPAT_MODEL", "").strip() or None
