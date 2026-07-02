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
        "/api/auth/register",
        "/api/auth/verify-email",
        "/api/auth/forgot-password",
        "/api/auth/reset-password",
        "/api/auth/status",
        "/api/auth/google/config",
        "/api/health",
        "/api/waitlist",
        "/api/roadmap",
    }
    public_pages = {"/", "/login", "/register", "/verify-email", "/forgot-password", "/reset-password", "/terms", "/roadmap", "/profile", "/sitemap.xml", "/robots.txt", "/og-export", "/googleea1223cfdcbe9db5.html", "/static/login.html", "/favicon.ico", "/static/manifest.json", "/static/sw.js", "/static/og-image.svg", "/auth/google", "/auth/google/callback"}
    if path.startswith("/shared/"):
        return await call_next(request)

    # Expire sessions that have passed their TTL
    expires_at = request.session.get("session_expires_at")
    if expires_at and request.session.get("auth_user"):
        try:
            if datetime.now(timezone.utc) > datetime.fromisoformat(expires_at):
                request.session.clear()
        except Exception:
            pass

    # Inactivity timeout — 30 minutes of no API/page activity
    _INACTIVITY_SECONDS = 30 * 60
    if request.session.get("auth_user"):
        last_active = request.session.get("last_active_at")
        now_ts = datetime.now(timezone.utc)
        if last_active:
            try:
                if (now_ts - datetime.fromisoformat(last_active)).total_seconds() > _INACTIVITY_SECONDS:
                    request.session.clear()
            except Exception:
                pass
        # Refresh on every authenticated request (skip static files to avoid noise)
        if not path.startswith("/static/"):
            request.session["last_active_at"] = now_ts.isoformat()

    if routes.auth_enabled() and not routes.is_authenticated(request):
        # Bearer-token routes bypass session check
        if path.startswith("/api/public/") and request.headers.get("Authorization", "").startswith("Bearer "):
            return await call_next(request)
        if path.startswith("/api/") and path not in public_api:
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
        if not path.startswith("/api/") and path not in public_pages and not path.startswith("/static/"):
            return RedirectResponse(url="/", status_code=302)

    return await call_next(request)


app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("VIDEOWATCH_SESSION_SECRET", "videowatch-dev-session-secret"),
    same_site="lax",
    max_age=30 * 24 * 3600,  # 30 days max; actual expiry enforced via session_expires_at
)

# ── Server‑side scheduler ─────────────────────────────────────────────────────

async def _scheduler():
    """
    Runs forever while the server is up.
    Checks every 30 s which sites are due for a scan and fires them.
    Also triggers a nightly DB backup once per UTC day.
    Each site has its own scan_interval (default 300 s = 5 min).
    """
    log.info("Scheduler started")
    _last_backup_date: str | None = None
    while True:
        await asyncio.sleep(30)
        # Nightly backup at the first scheduler tick after midnight UTC
        try:
            now_utc = datetime.now(timezone.utc)
            today = now_utc.strftime("%Y-%m-%d")
            if today != _last_backup_date:
                _last_backup_date = today
                from routes import _run_backup
                threading.Thread(target=_run_backup, daemon=True).start()
                # Weekly digest — send every Monday
                if now_utc.weekday() == 0:
                    from routes import send_weekly_digest
                    threading.Thread(target=send_weekly_digest, daemon=True).start()
        except Exception as e:
            log.error(f"Nightly backup error: {e}")
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
                    log.info(f"Scheduler: queuing auto-scan for {site['url']}")
                    from routes import _enqueue_scan
                    _enqueue_scan(site)
        except Exception as e:
            log.error(f"Scheduler error: {e}", exc_info=True)

# ── Lifespan (startup / shutdown) ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(application: FastAPI):
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
    if routes.auth_enabled() and not routes.is_authenticated(request):
        landing = STATIC_DIR / "landing.html"
        if landing.exists():
            return FileResponse(str(landing))
        # fallback to login if no landing page
        login = STATIC_DIR / "login.html"
        if login.exists():
            return FileResponse(str(login))
        raise HTTPException(404, "landing.html not found in static/")

    idx = STATIC_DIR / "index.html"
    if idx.exists():
        return FileResponse(str(idx))
    raise HTTPException(404, "index.html not found in static/")


@app.get("/login")
def login_page(request: Request):
    if routes.auth_enabled() and routes.is_authenticated(request):
        return RedirectResponse(url="/", status_code=302)
    login = STATIC_DIR / "login.html"
    if login.exists():
        return FileResponse(str(login))
    raise HTTPException(404, "login.html not found in static/")


@app.get("/register")
def register_page(request: Request):
    if routes.auth_enabled() and routes.is_authenticated(request):
        return RedirectResponse(url="/", status_code=302)
    page = STATIC_DIR / "register.html"
    if page.exists():
        return FileResponse(str(page))
    raise HTTPException(404, "register.html not found in static/")


@app.get("/verify-email")
def verify_email_page():
    page = STATIC_DIR / "verify-email.html"
    if page.exists():
        return FileResponse(str(page))
    raise HTTPException(404, "verify-email.html not found in static/")


@app.get("/forgot-password")
def forgot_password_page():
    page = STATIC_DIR / "forgot-password.html"
    if page.exists():
        return FileResponse(str(page))
    raise HTTPException(404, "forgot-password.html not found in static/")


@app.get("/reset-password")
def reset_password_page():
    page = STATIC_DIR / "reset-password.html"
    if page.exists():
        return FileResponse(str(page))
    raise HTTPException(404, "reset-password.html not found in static/")


@app.get("/googleea1223cfdcbe9db5.html")
def google_verify():
    page = STATIC_DIR / "googleea1223cfdcbe9db5.html"
    if page.exists():
        return FileResponse(str(page))
    raise HTTPException(404)


@app.get("/og-export")
def og_export_page():
    page = STATIC_DIR / "og-export.html"
    if page.exists():
        return FileResponse(str(page))
    raise HTTPException(404)


@app.get("/sitemap.xml")
def sitemap():
    from fastapi.responses import Response as FastResponse
    base = "https://videowatch.duckdns.org"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    urls = [
        (base + "/",        "weekly",  "1.0"),
        (base + "/register","monthly", "0.8"),
        (base + "/terms",   "yearly",  "0.3"),
    ]
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for loc, freq, pri in urls:
        xml += f"  <url><loc>{loc}</loc><lastmod>{today}</lastmod><changefreq>{freq}</changefreq><priority>{pri}</priority></url>\n"
    xml += "</urlset>"
    return FastResponse(content=xml, media_type="application/xml")


@app.get("/robots.txt")
def robots():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(
        "User-agent: *\nAllow: /\nDisallow: /api/\nSitemap: https://videowatch.duckdns.org/sitemap.xml\n"
    )


@app.get("/roadmap")
def roadmap_page():
    page = STATIC_DIR / "roadmap.html"
    if page.exists():
        return FileResponse(str(page))
    raise HTTPException(404, "roadmap.html not found in static/")


@app.get("/shared/collection/{token}")
def shared_collection_page(token: str):
    page = STATIC_DIR / "shared-collection.html"
    if page.exists():
        return FileResponse(str(page))
    raise HTTPException(404)


@app.get("/terms")
def terms_page():
    page = STATIC_DIR / "terms.html"
    if page.exists():
        return FileResponse(str(page))
    raise HTTPException(404, "terms.html not found in static/")


@app.get("/settings")
def settings_page(request: Request):
    if routes.auth_enabled() and not routes.is_authenticated(request):
        return RedirectResponse(url="/", status_code=302)

    page = STATIC_DIR / "settings.html"
    if page.exists():
        return FileResponse(str(page))
    raise HTTPException(404, "settings.html not found in static/")


@app.get("/profile")
def profile_page(request: Request):
    if routes.auth_enabled() and not routes.is_authenticated(request):
        return RedirectResponse(url="/", status_code=302)

    page = STATIC_DIR / "profile.html"
    if page.exists():
        return FileResponse(str(page))
    raise HTTPException(404, "profile.html not found in static/")

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
