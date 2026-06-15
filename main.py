import asyncio
import logging
import os
import socket
import sys
import webbrowser
from contextlib import asynccontextmanager

# Fix OpenMP conflict between PyTorch and faster-whisper
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import APP_DIR, COVERS_DIR, HOST, PORT
from database import init_db
from pipeline import router as pipeline_router
from routers import api, scan, tokutens as tokutens_router, games as games_router, metadata as metadata_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Suppress Uvicorn access logs
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def _quiet_proactor_connection_reset(loop, context):
    """Swallow the ConnectionResetError noise from Windows' ProactorEventLoop.

    When a client disconnects abruptly (mobile tab backgrounded, screen off,
    network blip) the peer RSTs the TCP connection. Asyncio still attempts a
    graceful ``socket.shutdown()`` in ``_ProactorBasePipeTransport._call_connection_lost``
    and trips WinError 10054. The connection is already torn down — purely
    post-close cleanup — so we filter just that exact callback + exception
    combo and let the default handler surface everything else."""
    exc = context.get("exception")
    callback = context.get("handle")
    if (
        isinstance(exc, ConnectionResetError)
        and "_call_connection_lost" in repr(callback)
    ):
        return
    loop.default_exception_handler(context)

STATIC_DIR = APP_DIR / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Quiet the post-close socket-shutdown errors on Windows. Has to happen
    # inside the running loop, so lifespan startup is the right place.
    if sys.platform == "win32":
        try:
            asyncio.get_running_loop().set_exception_handler(_quiet_proactor_connection_reset)
        except RuntimeError:
            pass
    logger.info("Initializing database...")
    await init_db()
    logger.info("Database ready.")
    yield


app = FastAPI(title="DramaCD Library Browser", version="0.1.0", lifespan=lifespan)

# Include routers
app.include_router(api.router)
app.include_router(scan.router)
app.include_router(pipeline_router)
app.include_router(tokutens_router.router)
app.include_router(games_router.router)
app.include_router(metadata_router.router)

# Serve cover art from data/covers/
COVERS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/covers", StaticFiles(directory=str(COVERS_DIR)), name="covers")

# Games wing covers — separate mount so a drama-CD cover and a game cover
# can never collide on basename.
from pathlib import Path as _Path
_GAMES_COVERS_DIR = _Path("data/games/covers")
_GAMES_COVERS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/data/games/covers", StaticFiles(directory=str(_GAMES_COVERS_DIR)), name="games_covers")

# Serve static files (JS, CSS)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def serve_index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/favicon.ico", include_in_schema=False)
async def serve_favicon():
    return FileResponse(str(STATIC_DIR / "favicon.svg"), media_type="image/svg+xml")


def get_local_ip() -> str:
    """Get the machine's local network IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    local_ip = get_local_ip()
    print()
    print("=" * 60)
    print("  DramaCD Library Browser")
    print("=" * 60)
    print(f"  Local:   http://localhost:{PORT}")
    if HOST in {"0.0.0.0", "::"}:
        print(f"  Network: http://{local_ip}:{PORT}")
    else:
        print("  Network: local-only (set DRAMACD_BIND_ALL=1 to expose on LAN)")
    print()
    print("  Open the above URL in your browser.")
    print("  Other devices on your network can use the Network URL.")
    print("=" * 60)
    print()

    webbrowser.open(f"http://localhost:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info", access_log=False)
