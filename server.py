# VideoWatch Server - Deployed on Oracle Cloud
"""
VideoWatch Backend — FastAPI + Playwright
Modular entry point. Bootstraps the application, schedules the background auto‑scanner task.
"""


import os
import sys
import logging
import asyncio
import warnings
import webbrowser           # <-- added
import threading            # <-- added
from datetime import datetime, timezone
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse, JSONResponse

# Fix Windows asyncio subprocess support for Playwright before importing async playwright
if sys.platform.startswith("win"):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

# Initialize logging
_LOG_FILE = Path(__file__).parent / "videowatch.log"
_log_handler = logging.FileHandler(_LOG_FILE, encoding="utf-8")
_log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(), _log_handler],
)
log = logging.getLogger(__name__)

# Initialize database
import db
db.init_db()

from db import get_db, DB_PATH
from scraper import scan_site
import routes
from routes import push_progress

app = FastAPI(title="VideoWatch API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(routes.router)


@app.middleware("http")
async def auth_gate(request, call_next):
    path = request.url.path
    public_api = {
        "/api/auth/login",
        "/api/auth/status",
        "/api/health",
    }
    public_pages = {"/", "/static/login.html", "/favicon.ico"}

    if routes.auth_enabled() and not routes.is_authenticated(request):
        if path.startswith("/api/") and path not in public_api:
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
        if not path.startswith("/api/") and path not in public_pages and not path.startswith("/static/"):
            return RedirectResponse(url="/", status_code=302)

    return await call_next(request)


app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("VIDEOWATCH_SESSION_SECRET", "videowatch-dev-session-secret"),
    same_site="lax",
)

# ── Server‑side scheduler ─────────────────────────────────────────────────────

async def _scheduler():
    """
    Runs forever while the server is up.
    Checks every 30 s which sites are due for a scan and fires them.
    Each site has its own scan_interval (default 300 s = 5 min).
    """
    log.info("Scheduler started")
    while True:
        await asyncio.sleep(30)
        try:
            with get_db() as db_conn:
                setting = db_conn.execute(
                    "SELECT value FROM app_settings WHERE key='autoscan_enabled'"
                ).fetchone()
                autoscan_enabled = (setting["value"] == "1") if setting else True
                if not autoscan_enabled:
                    continue
                sites = [dict(r) for r in db_conn.execute("SELECT * FROM sites")]
            now = datetime.now(timezone.utc)
            for site in sites:
                interval = int(site.get("scan_interval") or 300)
                last = site.get("last_scan")
                if last is None:
                    due = True
                else:
                    try:
                        due = (now - datetime.fromisoformat(last)).total_seconds() >= interval
                    except Exception:
                        due = True
                if due:
                    log.info(f"Scheduler: auto‑scanning {site['url']}")
                    await push_progress(f"AUTO_SCAN|{site['id']}|{site.get('name') or site['url']}")
                    try:
                        await scan_site(site, push_progress)
                    except Exception as e:
                        log.error(f"Scheduler scan failed for {site['url']}: {e}", exc_info=True)
                        await push_progress(f"SCHEDULER_ERROR|{site['id']}|{e}")
        except Exception as e:
            log.error(f"Scheduler error: {e}", exc_info=True)

# ── Lifespan (startup / shutdown) ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(application: FastAPI):
    routes.progress_queue = asyncio.Queue()
    scheduler_task = asyncio.create_task(_scheduler())
    log.info("VideoWatch started — scheduler running")
    yield
    scheduler_task.cancel()
    log.info("VideoWatch stopped")

app.router.lifespan_context = lifespan

# ── Static / frontend ─────────────────────────────────────────────────────────

STATIC_DIR = Path("static")
STATIC_DIR.mkdir(exist_ok=True)
try:
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
except Exception as e:
    log.warning(f"Could not mount static folder: {e}")

@app.get("/api/logs")
def get_logs(lines: int = 200):
    """Return the last N lines of the server log file as plain text."""
    try:
        text = _LOG_FILE.read_text(encoding="utf-8", errors="replace")
        tail = "\n".join(text.splitlines()[-lines:])
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(tail)
    except Exception as e:
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(f"Log file not found: {e}", status_code=404)


@app.get("/")
def root(request: Request):
    # Serve login page when auth is enabled and no active session exists.
    if routes.auth_enabled() and not routes.is_authenticated(request):
        login = STATIC_DIR / "login.html"
        if login.exists():
            return FileResponse(str(login))
        raise HTTPException(404, "login.html not found in static/")

    idx = STATIC_DIR / "index.html"
    if idx.exists():
        return FileResponse(str(idx))
    raise HTTPException(404, "index.html not found in static/")


@app.get("/settings")
def settings_page(request: Request):
    if routes.auth_enabled() and not routes.is_authenticated(request):
        return RedirectResponse(url="/", status_code=302)

    page = STATIC_DIR / "settings.html"
    if page.exists():
        return FileResponse(str(page))
    raise HTTPException(404, "settings.html not found in static/")

if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("HOST", "127.0.0.1")          # <-- changed default host
    port = int(os.environ.get("PORT", "8000"))
    reload_flag = os.environ.get("RELOAD", "false").lower() in ("1", "true", "yes")

    # ---------------------------------------------------------------
    # Open the default browser (Edge if it’s your system default) to the
    # server URL as soon as the server starts. A short timer gives uvicorn
    # a moment to bind the port before the browser attempts the request.
    # ---------------------------------------------------------------
    def _open_browser():
        # Use localhost which works regardless of the bind address
        webbrowser.open(f"http://127.0.0.1:{port}")

    threading.Timer(1.0, _open_browser).start()
    # ---------------------------------------------------------------

    uvicorn.run("server:app", host=host, port=port, reload=reload_flag)
