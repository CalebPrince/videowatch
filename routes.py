import os
import sys
import json
import hashlib
import base64
import logging
import asyncio
import hmac
import secrets
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlunparse, urljoin

import httpx
from fastapi import APIRouter, HTTPException, BackgroundTasks, Query, status, Response, Request
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel
import mimetypes
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

# Directory to store video files inside the current project workspace
VIDEOS_DIR = Path(__file__).resolve().parent / "videos"
VIDEOS_DIR.mkdir(exist_ok=True)
LEGACY_VIDEOS_DIR = Path(__file__).resolve().parent.parent / "videos"
import ipaddress
import time
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from db import get_db, write_lock, DB_PATH

# ── Rate limiting ─────────────────────────────────────────────────────────────
_rate_limit_store: dict[str, list[float]] = {}

def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

def _check_rate_limit(ip: str, max_attempts: int = 5, window_seconds: int = 300) -> None:
    now = time.monotonic()
    hits = _rate_limit_store.get(ip, [])
    hits = [t for t in hits if now - t < window_seconds]
    hits.append(now)
    _rate_limit_store[ip] = hits
    if len(hits) > max_attempts:
        retry_after = int(window_seconds - (now - hits[0]))
        raise HTTPException(
            status_code=429,
            detail=f"Too many attempts. Try again in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )

# ── Email / SMTP ───────────────────────────────────────────────────────────────
_SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
_SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
_SMTP_USER = os.environ.get("SMTP_USER", "")
_SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
_APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000")

def _email_configured() -> bool:
    return bool(_SMTP_USER and _SMTP_PASSWORD)

def _send_verification_email(to_address: str, username: str, token: str) -> None:
    verify_url = f"{_APP_BASE_URL}/verify-email?token={token}"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Verify your VideoWatch account"
    msg["From"] = f"VideoWatch <{_SMTP_USER}>"
    msg["To"] = to_address
    msg["Reply-To"] = _SMTP_USER
    msg["X-Mailer"] = "VideoWatch"
    msg["Message-ID"] = f"<verify-{token[:16]}@videowatch>"

    text = (
        f"Hi {username},\n\n"
        f"Click the link below to verify your email address:\n{verify_url}\n\n"
        f"This link expires in 24 hours.\n\nIf you didn't register, ignore this email."
    )
    html = f"""
    <div style="font-family:sans-serif;max-width:480px;margin:auto">
      <h2 style="color:#0f766e">Verify your VideoWatch account</h2>
      <p>Hi <strong>{username}</strong>,</p>
      <p>Click the button below to verify your email address and activate your account.</p>
      <a href="{verify_url}"
         style="display:inline-block;padding:12px 24px;background:#0f766e;color:#fff;
                text-decoration:none;border-radius:6px;font-weight:600;margin:16px 0">
        Verify Email
      </a>
      <p style="color:#6b7280;font-size:0.85rem">
        Link expires in 24 hours. If you didn't register, you can safely ignore this email.
      </p>
    </div>
    """
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(_SMTP_USER, _SMTP_PASSWORD)
        smtp.sendmail(_SMTP_USER, to_address, msg.as_string())

from scraper import (
    scan_site,
    scan_all_sites,
    short_id,
    now_iso,
    normalize_url,
    cookie_path
)

# ── Logging & Paths ───────────────────────────────────────────────────────────
log = logging.getLogger(__name__)
THUMBS_DIR = Path("thumbcache")
THUMBS_DIR.mkdir(exist_ok=True)


def _migrate_legacy_videos_dir(target_dir: Path | None = None, legacy_dir: Path | None = None) -> dict[str, int]:
    target = Path(target_dir or VIDEOS_DIR)
    legacy = Path(legacy_dir or LEGACY_VIDEOS_DIR)
    result = {"moved": 0, "skipped": 0, "errors": 0}

    if legacy.resolve() == target.resolve() or not legacy.exists() or not legacy.is_dir():
        return result

    target.mkdir(exist_ok=True)
    for entry in legacy.iterdir():
        if not entry.is_file():
            continue
        destination = target / entry.name
        if destination.exists():
            result["skipped"] += 1
            continue
        try:
            shutil.move(str(entry), str(destination))
            result["moved"] += 1
        except Exception as exc:
            result["errors"] += 1
            log.warning(f"Legacy video migration failed for {entry}: {exc}")

    if result["moved"]:
        log.info(
            "Migrated %s downloaded video(s) from %s to %s",
            result["moved"],
            legacy,
            target,
        )
    return result


_migrate_legacy_videos_dir()

router = APIRouter()

# ── SSE Pub-Sub (one queue per connected client) ──────────────────────────────
progress_queue: asyncio.Queue = None   # kept for legacy imports; not used internally
_sse_subscribers: list[asyncio.Queue] = []

async def push_progress(msg: str):
    """Broadcast a scan progress message to every connected SSE client."""
    for q in list(_sse_subscribers):
        await q.put(msg)

# ── Pydantic Models ───────────────────────────────────────────────────────────

class SiteIn(BaseModel):
    url:           str
    name:          str = ""
    group_name:    str = ""
    max_pages:     int = 1
    scan_interval: int = 300   # seconds
    rule_include_keywords: str = ""
    rule_exclude_keywords: str = ""
    rule_min_duration: int = 0
    scan_profile: str = "balanced"
    notify_enabled: bool = True

class SitePatch(BaseModel):
    name:          str | None = None
    group_name:    str | None = None
    max_pages:     int | None = None
    scan_interval: int | None = None
    rule_include_keywords: str | None = None
    rule_exclude_keywords: str | None = None
    rule_min_duration: int | None = None
    scan_profile: str | None = None
    notify_enabled: bool | None = None

class MarkSeenIn(BaseModel):
    site_id: str | None = None

class AutomationToggleIn(BaseModel):
    enabled: bool

class LoginIn(BaseModel):
    username: str
    password: str


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str


class UserCreateIn(BaseModel):
    username: str
    password: str
    role: str = "viewer"


class RegisterIn(BaseModel):
    username: str
    email: str
    password: str
    confirm_password: str


class UserRolePatchIn(BaseModel):
    role: str
    active: bool | None = None


class NotificationSettingsIn(BaseModel):
    enabled: bool
    webhook_url: str = ""
    digest_minutes: int = 0


class VideoStatePatch(BaseModel):
    is_favorite: bool | None = None
    is_archived: bool | None = None
    is_ignored: bool | None = None
    is_watched: bool | None = None


class BulkVideoActionIn(BaseModel):
    video_ids: list[str]
    action: str  # "favorite", "unfavorite", "archive", "unarchive", "ignore", "unignore", "mark_seen", "mark_watched", "mark_unwatched"


class MergeDuplicateIn(BaseModel):
    keep_id: str
    remove_id: str


def _browser_headers(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "Referer": parsed.scheme + "://" + parsed.netloc + "/",
    }


def _classify_media(final_url: str, content_type: str) -> str:
    lower_url = (final_url or "").lower()
    lower_ct = (content_type or "").lower()
    if ".m3u8" in lower_url or "mpegurl" in lower_ct:
        return "hls"
    if any(ext in lower_url for ext in (".mp4", ".webm", ".ogg", ".mov", ".mkv")):
        return "direct"
    if lower_ct.startswith("video/"):
        return "direct"
    return "none"


def _guess_media_extension(url: str, content_type: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".mp4", ".webm", ".ogg", ".mov", ".mkv"}:
        return suffix
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    return {
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "video/ogg": ".ogg",
        "video/quicktime": ".mov",
        "video/x-matroska": ".mkv",
    }.get(ct, ".mp4")


def _init_resolver_diagnostics(url: str) -> dict:
    return {
        "requested_url": url,
        "final_url": None,
        "http_content_type": None,
        "http_status": None,
        "html_candidates": 0,
        "browser_used": False,
        "browser_site_id": None,
        "browser_cookies_loaded": False,
        "browser_video_tag_src": False,
        "browser_network_candidates": 0,
        "browser_json_candidates": 0,
        "browser_html_candidates": 0,
        "browser_error": None,
    }


def _format_resolver_diagnostics(diagnostics: dict | None) -> str:
    if not diagnostics:
      return ""
    parts = []
    if diagnostics.get("final_url"):
        parts.append(f"final={diagnostics['final_url']}")
    if diagnostics.get("http_status"):
        parts.append(f"http={diagnostics['http_status']}")
    if diagnostics.get("http_content_type"):
        parts.append(f"content-type={diagnostics['http_content_type']}")
    parts.append(f"html-candidates={diagnostics.get('html_candidates', 0)}")
    if diagnostics.get("browser_used"):
        parts.append(
            "browser="
            f"network:{diagnostics.get('browser_network_candidates', 0)},"
            f"json:{diagnostics.get('browser_json_candidates', 0)},"
            f"html:{diagnostics.get('browser_html_candidates', 0)},"
            f"video-tag:{'yes' if diagnostics.get('browser_video_tag_src') else 'no'},"
            f"cookies:{'yes' if diagnostics.get('browser_cookies_loaded') else 'no'}"
        )
    if diagnostics.get("browser_error"):
        parts.append(f"browser-error={diagnostics['browser_error']}")
    return "; ".join(parts)


def _url_variants(url: str | None) -> list[str]:
    raw = (url or "").strip()
    if not raw:
        return []
    variants: list[str] = []
    for candidate in (raw, raw.rstrip("/"), raw.rstrip("/") + "/"):
        if not candidate:
            continue
        if candidate not in variants:
            variants.append(candidate)
    return variants


def _build_resolution_candidates(url: str | None, embed_url: str | None) -> list[str]:
    candidates: list[str] = []
    for source in (_url_variants(url), _url_variants(embed_url)):
        for candidate in source:
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def _candidate_media_urls(text: str, base_url: str) -> list[str]:
    if not text:
        return []
    patterns = [
        r'<meta[^>]+property=["\']og:video(?::url)?["\'][^>]+content=["\']([^"\']+)["\']',
        r'<source[^>]+src=["\']([^"\']+)["\']',
        r'"(https?://[^"\']+\.(?:mp4|webm|ogg|mov|m3u8|mkv)(?:\?[^"\']*)?)"',
        r"'(https?://[^\"']+\.(?:mp4|webm|ogg|mov|m3u8|mkv)(?:\?[^\"']*)?)'",
        r'"file"\s*:\s*"([^"\n]+\.(?:mp4|webm|ogg|mov|m3u8|mkv)(?:\?[^"\n]*)?)"',
        r'"src"\s*:\s*"([^"\n]+\.(?:mp4|webm|ogg|mov|m3u8|mkv)(?:\?[^"\n]*)?)"',
    ]
    found: list[str] = []
    for pat in patterns:
        for match in re.finditer(pat, text, flags=re.I):
            candidate = (match.group(1) or "").strip().replace("\\/", "/")
            resolved = urljoin(base_url, candidate)
            if urlparse(resolved).scheme in {"http", "https"}:
                found.append(resolved)
    return found


def _path_tokens(url: str) -> set[str]:
    path = (urlparse(url).path or "").lower()
    return {token for token in re.split(r"[^a-z0-9]+", path) if len(token) >= 3}


def _text_tokens(text: str | None) -> set[str]:
    raw = (text or "").lower()
    return {token for token in re.split(r"[^a-z0-9]+", raw) if len(token) >= 3}


def _score_media_candidate(candidate_url: str, kind: str, reason: str, page_url: str, requested_url: str, expected_title: str | None = None) -> tuple[int, int, int, int, int, int]:
    candidate_host = (urlparse(candidate_url).netloc or "").lower()
    page_host = (urlparse(page_url).netloc or "").lower()
    requested_host = (urlparse(requested_url).netloc or "").lower()
    candidate_tokens = _path_tokens(candidate_url)
    page_tokens = _path_tokens(page_url)
    requested_tokens = _path_tokens(requested_url)
    title_tokens = _text_tokens(expected_title)

    source_bonus = 0
    if reason == "resolved from browser DOM":
        source_bonus = 500
    elif reason == "resolved from browser HTML":
        source_bonus = 300
    elif reason == "resolved from browser JSON":
        source_bonus = 250
    elif reason == "resolved from browser network":
        source_bonus = 150

    kind_bonus = 40 if kind == "direct" else 10
    host_bonus = 0
    if candidate_host and candidate_host == page_host:
        host_bonus += 120
    elif candidate_host and page_host and candidate_host.endswith("." + page_host):
        host_bonus += 90
    if candidate_host and candidate_host == requested_host:
        host_bonus += 40

    overlap = len(candidate_tokens & (page_tokens | requested_tokens))
    title_overlap = len(candidate_tokens & title_tokens)

    path = candidate_url.lower()
    penalty_terms = ("/ad", "ads", "trailer", "teaser", "preview", "promo", "vast", "preroll", "sample")
    penalty = sum(120 for term in penalty_terms if term in path)
    if reason == "resolved from browser network" and overlap == 0 and title_overlap == 0:
        penalty += 220
    if title_tokens and title_overlap == 0 and reason == "resolved from browser network":
        penalty += 140

    return (
        source_bonus + kind_bonus + host_bonus + overlap * 35 + title_overlap * 60 - penalty,
        title_overlap,
        overlap,
        host_bonus,
        source_bonus,
        -penalty,
    )


def _choose_best_media_candidate(candidates: list[tuple[str, str, str]], page_url: str, requested_url: str, expected_title: str | None = None) -> tuple[str, str, str] | None:
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: _score_media_candidate(item[0], item[1], item[2], page_url, requested_url, expected_title),
    )


def _find_site_id_for_url(url: str) -> str | None:
    target_host = (urlparse(url).netloc or "").lower()
    if not target_host:
        return None
    with get_db() as db:
        rows = db.execute("SELECT id, url FROM sites").fetchall()
    for row in rows:
        site_host = (urlparse(row["url"]).netloc or "").lower()
        if not site_host:
            continue
        if target_host == site_host or target_host.endswith("." + site_host) or site_host.endswith("." + target_host):
            return row["id"]
    return None


async def _resolve_video_source_with_browser(url: str, diagnostics: dict | None = None, expected_title: str | None = None) -> dict:
    headers = _browser_headers(url)
    candidates: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    site_id = _find_site_id_for_url(url)
    diagnostics = diagnostics or _init_resolver_diagnostics(url)
    diagnostics["browser_used"] = True
    diagnostics["browser_site_id"] = site_id

    def remember(candidate_url: str, kind: str, reason: str):
        if not candidate_url or candidate_url in seen or kind == "none":
            return
        seen.add(candidate_url)
        candidates.append((candidate_url, kind, reason))

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            context = await browser.new_context(
                user_agent=headers["User-Agent"],
                viewport={"width": 1440, "height": 900},
                ignore_https_errors=True,
            )
            try:
                if site_id:
                    cp = cookie_path(site_id)
                    if cp.exists():
                        try:
                            saved = json.loads(cp.read_text())
                            await context.add_cookies(saved)
                            diagnostics["browser_cookies_loaded"] = True
                        except Exception as exc:
                            log.warning(f"Resolver cookie restore failed for {site_id}: {exc}")

                page = await context.new_page()
                await Stealth().apply_stealth_async(page)

                async def on_response(response):
                    try:
                        response_url = str(response.url)
                        content_type = (response.headers.get("content-type") or "").lower()
                        kind = _classify_media(response_url, content_type)
                        if kind != "none":
                            diagnostics["browser_network_candidates"] += 1
                            remember(response_url, kind, "resolved from browser network")
                            return
                        if "json" in content_type and response.status == 200:
                            text = await response.text()
                            for candidate in _candidate_media_urls(text, response_url):
                                diagnostics["browser_json_candidates"] += 1
                                remember(candidate, _classify_media(candidate, ""), "resolved from browser JSON")
                    except Exception:
                        return

                page.on("response", lambda response: asyncio.create_task(on_response(response)))

                try:
                    await page.goto(url, timeout=45000, wait_until="networkidle")
                except Exception:
                    await page.goto(url, timeout=30000, wait_until="domcontentloaded")

                try:
                    media_src = await page.evaluate(
                        """
                        () => {
                            const video = document.querySelector('video');
                            if (!video) return '';
                            return video.currentSrc || video.src || '';
                        }
                        """
                    )
                    if media_src:
                        resolved = urljoin(page.url, media_src)
                        diagnostics["browser_video_tag_src"] = True
                        remember(resolved, _classify_media(resolved, ""), "resolved from browser DOM")
                except Exception:
                    pass

                try:
                    html = await page.content()
                    for candidate in _candidate_media_urls(html, page.url):
                        diagnostics["browser_html_candidates"] += 1
                        remember(candidate, _classify_media(candidate, ""), "resolved from browser HTML")
                except Exception:
                    pass

                await page.wait_for_timeout(1500)
                diagnostics["final_url"] = page.url

                if site_id:
                    try:
                        cookies = await context.cookies()
                        cookie_path(site_id).write_text(json.dumps(cookies))
                    except Exception as exc:
                        log.warning(f"Resolver cookie save failed for {site_id}: {exc}")
            finally:
                await context.close()
                await browser.close()
    except Exception as exc:
        log.warning(f"Browser resolver failed for {url}: {exc}")
        diagnostics["browser_error"] = str(exc)
        return {"resolved_url": None, "kind": "none", "reason": "browser resolver error", "headers": headers, "diagnostics": diagnostics}

    if candidates:
        chosen = _choose_best_media_candidate(
            candidates,
            diagnostics.get("final_url") or url,
            diagnostics.get("requested_url") or url,
            expected_title,
        )
        return {
            "resolved_url": chosen[0],
            "kind": chosen[1],
            "reason": chosen[2],
            "headers": headers,
            "diagnostics": diagnostics,
        }

    return {"resolved_url": None, "kind": "none", "reason": "no playable media discovered", "headers": headers, "diagnostics": diagnostics}


async def _resolve_video_source_impl(url: str, expected_title: str | None = None) -> dict:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="Invalid URL scheme")

    allowed_hosts = get_allowed_hosts()
    if not is_url_allowed(url, allowed_hosts):
        raise HTTPException(status_code=400, detail="URL host is not allowed")

    headers = _browser_headers(url)
    diagnostics = _init_resolver_diagnostics(url)

    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            r = await client.get(url, headers=headers)

        final_url = str(r.url)
        ct = (r.headers.get("content-type") or "").lower()
        diagnostics["final_url"] = final_url
        diagnostics["http_content_type"] = ct
        diagnostics["http_status"] = r.status_code
        kind = _classify_media(final_url, ct)

        if kind != "none":
            return {"resolved_url": final_url, "kind": kind, "reason": "resolved from final URL", "headers": headers, "diagnostics": diagnostics}

        html = r.text or ""
        if not html:
            return {"resolved_url": None, "kind": "none", "reason": "empty response", "headers": headers, "diagnostics": diagnostics}

        for candidate in _candidate_media_urls(html, final_url):
            diagnostics["html_candidates"] += 1
            return {
                "resolved_url": candidate,
                "kind": _classify_media(candidate, ""),
                "reason": "resolved from page metadata",
                "headers": headers,
                "diagnostics": diagnostics,
            }

        browser_result = await _resolve_video_source_with_browser(final_url, diagnostics, expected_title)
        if browser_result.get("resolved_url"):
            return browser_result
        return {
            "resolved_url": None,
            "kind": "none",
            "reason": browser_result.get("reason", "no playable media discovered"),
            "headers": headers,
            "diagnostics": browser_result.get("diagnostics", diagnostics),
        }

    except HTTPException:
        raise
    except Exception as e:
        log.warning(f"Resolve video source failed for {url}: {e}")
        diagnostics["browser_error"] = str(e)
        return {"resolved_url": None, "kind": "none", "reason": "resolver error", "headers": headers, "diagnostics": diagnostics}


async def _download_media_file(video_id: str, resolved_url: str, headers: dict[str, str]) -> dict:
    # Use yt-dlp based downloader for IDM‑style parallel downloads.
    from .downloader import download_video as yt_download
    # yt-dlp handles parallel fragment downloading and resume support.
    saved_path = yt_download(resolved_url)
    if not saved_path:
        raise HTTPException(status_code=502, detail="yt‑dlp failed to download video")
    filename = saved_path.name
    # Determine a simple content type based on file extension.
    ext = saved_path.suffix.lower()
    content_type = {
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mkv": "video/x-matroska",
    }.get(ext, "application/octet-stream")
    return {"filename": filename, "content_type": content_type}


def _update_video_download_metadata(video_id: str, **fields):
    if not fields:
        return
    columns = ", ".join(f"{key}=?" for key in fields)
    values = list(fields.values()) + [video_id]
    with write_lock:
        with get_db() as db:
            db.execute(f"UPDATE videos SET {columns} WHERE id=?", values)
            db.commit()


def auth_enabled() -> bool:
    raw = os.environ.get("VIDEOWATCH_AUTH_ENABLED", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def expected_auth_user() -> str:
    return os.environ.get("VIDEOWATCH_AUTH_USERNAME", "admin")


def expected_auth_password() -> str:
    return os.environ.get("VIDEOWATCH_AUTH_PASSWORD", "admin123")


def _read_setting(key: str) -> str | None:
    with get_db() as db:
        row = db.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _write_setting(key: str, value: str):
    with write_lock:
        with get_db() as db:
            db.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            db.commit()


def _current_auth_username() -> str:
    return _read_setting("auth_username") or expected_auth_user()


def _pbkdf2_hash(password: str, salt_b64: str) -> str:
    salt = base64.b64decode(salt_b64.encode("ascii"))
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return base64.b64encode(digest).decode("ascii")


def _set_hashed_password(username: str, raw_password: str):
    salt = secrets.token_bytes(16)
    salt_b64 = base64.b64encode(salt).decode("ascii")
    hash_b64 = _pbkdf2_hash(raw_password, salt_b64)
    _write_setting("auth_username", username)
    _write_setting("auth_password_salt", salt_b64)
    _write_setting("auth_password_hash", hash_b64)


VALID_ROLES = {"super_admin", "admin", "viewer"}


def _sanitize_role(role: str | None) -> str:
    r = (role or "viewer").strip().lower()
    return r if r in VALID_ROLES else "viewer"


def _create_user_record(username: str, raw_password: str, role: str):
    user = (username or "").strip()
    if not user:
        raise HTTPException(400, "Username is required")
    if not raw_password or len(raw_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")

    salt = secrets.token_bytes(16)
    salt_b64 = base64.b64encode(salt).decode("ascii")
    hash_b64 = _pbkdf2_hash(raw_password, salt_b64)
    now = now_iso()

    with write_lock:
        with get_db() as db:
            db.execute(
                "INSERT INTO users (username, password_salt, password_hash, role, active, created_at, updated_at) "
                "VALUES (?,?,?,?,1,?,?)",
                (user, salt_b64, hash_b64, _sanitize_role(role), now, now),
            )
            db.commit()


def _get_user(username: str) -> dict | None:
    with get_db() as db:
        row = db.execute(
            "SELECT username, password_salt, password_hash, role, active FROM users WHERE username=?",
            (username,),
        ).fetchone()
    return dict(row) if row else None


def _ensure_default_admin_user():
    admin_user = expected_auth_user().strip() or "admin"
    with get_db() as db:
        row = db.execute("SELECT 1 FROM users WHERE username=?", (admin_user,)).fetchone()
    if not row:
        try:
            _create_user_record(admin_user, expected_auth_password(), "super_admin")
            log.info("Bootstrapped default admin user into users table")
        except Exception as e:
            log.warning(f"Could not bootstrap default admin user: {e}")
    # Upgrade the env-configured admin to super_admin (one-time migration)
    # and assign any ownerless sites to them.
    try:
        with write_lock:
            with get_db() as db:
                db.execute(
                    "UPDATE users SET role='super_admin', updated_at=? WHERE username=? AND role='admin'",
                    (now_iso(), admin_user),
                )
                db.execute(
                    "UPDATE sites SET owner=? WHERE owner IS NULL OR owner=''",
                    (admin_user,),
                )
                db.commit()
    except Exception as e:
        log.warning(f"Could not migrate default admin / site owners: {e}")


def is_authenticated(request: Request) -> bool:
    if not auth_enabled():
        return True
    return bool(request.session.get("auth_user"))


def current_role(request: Request) -> str:
    role = request.session.get("auth_role")
    if role:
        return role
    user = request.session.get("auth_user")
    if user:
        row = _get_user(user)
        if row:
            return _sanitize_role(row.get("role"))
    return _read_setting("auth_default_role") or "admin"


def require_admin(request: Request):
    if current_role(request) not in {"admin", "super_admin"}:
        raise HTTPException(403, "Admin role required")


def is_super_admin(request: Request) -> bool:
    if not auth_enabled():
        return True
    return current_role(request) == "super_admin"


def current_user(request: Request) -> str | None:
    if not auth_enabled():
        return None
    return request.session.get("auth_user")


def validate_credentials(username: str, password: str) -> bool:
    _ensure_default_admin_user()

    user = _get_user(username)
    if user and int(user.get("active") or 0) == 1:
        candidate_hash = _pbkdf2_hash(password, user["password_salt"])
        return hmac.compare_digest(candidate_hash, user["password_hash"])

    # Legacy fallback for older instances that only used app_settings/env auth.
    effective_user = _current_auth_username()
    if not hmac.compare_digest(username, effective_user):
        return False
    saved_hash = _read_setting("auth_password_hash")
    saved_salt = _read_setting("auth_password_salt")
    if saved_hash and saved_salt:
        candidate_hash = _pbkdf2_hash(password, saved_salt)
        return hmac.compare_digest(candidate_hash, saved_hash)

    exp_pass = expected_auth_password()
    return hmac.compare_digest(password, exp_pass)


def _notify_scan_summary(site: dict, found: int, added: int):
    enabled = (_read_setting("notify_enabled") or "0") == "1"
    webhook = (_read_setting("notify_webhook_url") or "").strip()
    site_notify = site.get("notify_enabled")
    site_notify_off = site_notify is not None and int(site_notify) == 0
    if not enabled or not webhook or added <= 0 or site_notify_off:
        return

    site_label = site.get("name") or site.get("url") or site.get("id")
    payload = {
        "text": f"VideoWatch: {site_label} scan complete - {added} new video(s), {found} found.",
        "site_id": site.get("id"),
        "site": site_label,
        "found": found,
        "added": added,
        "time": now_iso(),
    }
    try:
        httpx.post(webhook, json=payload, timeout=8.0)
    except Exception as e:
        log.warning(f"Notification webhook failed: {e}")

# ── Helper for SSRF verification ──────────────────────────────────────────────

def get_allowed_hosts() -> set[str]:
    """Retrieves lowercased netloc/domains of all currently monitored sites."""
    hosts = set()
    try:
        with get_db() as db:
            rows = db.execute("SELECT url FROM sites").fetchall()
            for r in rows:
                parsed = urlparse(r["url"])
                if parsed.netloc:
                    hosts.add(parsed.netloc.lower())
    except Exception as e:
        log.error(f"Error fetching allowed hosts: {e}")
    return hosts

def is_url_allowed(url: str, allowed_hosts: set[str]) -> bool:
    """Validates if a URL is from a safe/expected video platform or monitored site."""
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        if not netloc:
            return False

        # Whitelist of video CDNs/platforms
        ALLOWED_PLATFORMS = {
            "img.youtube.com", "i.ytimg.com", "youtube.com", "www.youtube.com",
            "vimeo.com", "player.vimeo.com", "f.vimeocdn.com",
            "dailymotion.com", "www.dailymotion.com", "thumbnail.video.dailymotion.com",
            "twitch.tv", "www.twitch.tv", "static-cdn.jtvnw.net",
        }
        if any(netloc == domain or netloc.endswith("." + domain) for domain in ALLOWED_PLATFORMS):
            return True

        # Check registered monitored sites (including subdomains)
        for host in allowed_hosts:
            if netloc == host or netloc.endswith("." + host):
                return True

        return False
    except Exception:
        return False

# ── API Endpoints ─────────────────────────────────────────────────────────────

@router.get("/api/sites")
def list_sites(request: Request):
    with get_db() as db:
        if is_super_admin(request):
            sites = [dict(r) for r in db.execute("SELECT * FROM sites ORDER BY added_at DESC")]
        else:
            user = current_user(request) or ""
            sites = [dict(r) for r in db.execute(
                "SELECT * FROM sites WHERE owner=? ORDER BY added_at DESC", (user,)
            )]
        for s in sites:
            s["new_count"] = db.execute(
                "SELECT COUNT(*) FROM videos WHERE site_id=? AND is_new=1",
                (s["id"],)).fetchone()[0]
            s["total_count"] = db.execute(
                "SELECT COUNT(*) FROM videos WHERE site_id=?",
                (s["id"],)).fetchone()[0]
            s["has_scan_history"] = bool(db.execute(
                "SELECT 1 FROM scan_log WHERE site_id=? LIMIT 1",
                (s["id"],),
            ).fetchone())
            last_log = db.execute(
                "SELECT message FROM scan_log WHERE site_id=? ORDER BY scanned_at DESC LIMIT 1",
                (s["id"],)).fetchone()
            s["has_error"] = bool(last_log and last_log["message"].startswith("ERROR"))
            
            if s["last_scan"]:
                try:
                    elapsed = (datetime.now(timezone.utc) -
                               datetime.fromisoformat(s["last_scan"])).total_seconds()
                    s["next_scan_in"] = max(0, int((s["scan_interval"] or 300) - elapsed))
                except Exception:
                    # Keep startup/site list resilient even if legacy timestamp text is malformed.
                    s["next_scan_in"] = 0
            else:
                s["next_scan_in"] = 0
    return sites

@router.post("/api/sites")
def add_site(body: SiteIn, request: Request):
    url = body.url.strip()
    if not url.startswith("http"):
        raise HTTPException(400, "URL must start with http:// or https://")
    
    _p = urlparse(url)
    _last = _p.path.rstrip("/").split("/")[-1].lower() if _p.path else ""
    _LISTING = {
        "videos","scenes","movies","episodes","clips","content",
        "latest","latest-updates","top-rated","most-popular",
        "categories","models","model","pornstars","tags","channels",
        "studios","networks","search","girls","guys","performers",
    }
    if (_p.path and _p.path != "/" and not _p.path.endswith("/")
            and not _p.query and _last not in _LISTING):
        url = url + "/"
        
    max_pages = max(1, min(body.max_pages, 20))
    scan_interval = max(60, body.scan_interval)
    rule_min_duration = max(0, body.rule_min_duration or 0)
    profile = (body.scan_profile or "balanced").strip().lower()
    if profile not in {"fast", "balanced", "deep"}:
        profile = "balanced"
    site_id = short_id(url)
    with write_lock:
        with get_db() as db:
            if db.execute("SELECT id FROM sites WHERE url=?", (url,)).fetchone():
                raise HTTPException(409, "Site already monitored")
            notify_enabled = 1 if body.notify_enabled else 0
            owner = current_user(request) or (expected_auth_user().strip() or "admin")
            db.execute(
                "INSERT INTO sites (id, url, name, group_name, added_at, max_pages, scan_interval, "
                "rule_include_keywords, rule_exclude_keywords, rule_min_duration, scan_profile, notify_enabled, owner) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    site_id,
                    url,
                    body.name.strip(),
                    body.group_name.strip(),
                    now_iso(),
                    max_pages,
                    scan_interval,
                    (body.rule_include_keywords or "").strip(),
                    (body.rule_exclude_keywords or "").strip(),
                    rule_min_duration,
                    profile,
                    notify_enabled,
                    owner,
                ))
            # Auto-enable auto-scan when the very first site is added
            site_count = db.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
            if site_count == 1:
                db.execute(
                    "INSERT INTO app_settings (key, value) VALUES ('autoscan_enabled', '1') "
                    "ON CONFLICT(key) DO UPDATE SET value='1'",
                )
            db.commit()
    return {"id": site_id, "url": url, "name": body.name,
            "group_name": body.group_name, "max_pages": max_pages, "scan_interval": scan_interval,
            "rule_include_keywords": (body.rule_include_keywords or "").strip(),
            "rule_exclude_keywords": (body.rule_exclude_keywords or "").strip(),
            "rule_min_duration": rule_min_duration,
            "scan_profile": profile, "notify_enabled": notify_enabled}


@router.post("/api/auth/login")
def auth_login(body: LoginIn, request: Request):
    _check_rate_limit(_client_ip(request))
    _ensure_default_admin_user()
    if not auth_enabled():
        request.session["auth_user"] = body.username or "local"
        row = _get_user(request.session["auth_user"])
        request.session["auth_role"] = _sanitize_role(row.get("role") if row else None) or "admin"
        return {
            "ok": True,
            "authenticated": True,
            "user": request.session["auth_user"],
            "role": request.session["auth_role"],
        }

    if not validate_credentials(body.username, body.password):
        raise HTTPException(401, "Invalid username or password")

    row = _get_user(body.username)
    # Block login if email verification is configured and not yet verified
    if _email_configured() and row and not row.get("email_verified"):
        # super_admin bootstrapped accounts are always exempt
        if row.get("role") != "super_admin":
            raise HTTPException(403, "Please verify your email before logging in. Check your inbox.")

    request.session["auth_user"] = body.username
    request.session["auth_role"] = _sanitize_role(row.get("role") if row else None) or "admin"
    return {
        "ok": True,
        "authenticated": True,
        "user": body.username,
        "role": request.session["auth_role"],
    }


@router.post("/api/auth/logout")
def auth_logout(request: Request):
    request.session.clear()
    return {"ok": True, "authenticated": False}


@router.post("/api/auth/register")
def auth_register(body: RegisterIn, request: Request):
    _check_rate_limit(_client_ip(request), max_attempts=3, window_seconds=300)
    if not auth_enabled():
        raise HTTPException(400, "Registration is not available when auth is disabled")
    username = (body.username or "").strip()
    email = (body.email or "").strip().lower()
    if not username or len(username) < 3:
        raise HTTPException(400, "Username must be at least 3 characters")
    if not re.match(r'^[a-zA-Z0-9_.-]+$', username):
        raise HTTPException(400, "Username may only contain letters, numbers, _ . -")
    if not email or not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        raise HTTPException(400, "A valid email address is required")
    if not body.password or len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    if body.password != body.confirm_password:
        raise HTTPException(400, "Passwords do not match")
    with get_db() as db:
        if db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise HTTPException(409, "Username already taken")
        if db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            raise HTTPException(409, "An account with that email already exists")
    _create_user_record(username, body.password, "viewer")
    # Store email and set verified status
    from datetime import timedelta
    expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    token = secrets.token_urlsafe(32)
    with write_lock:
        with get_db() as db:
            db.execute("UPDATE users SET email=?, email_verified=0 WHERE username=?", (email, username))
            db.execute(
                "INSERT OR REPLACE INTO email_verifications (token, username, expires_at) VALUES (?,?,?)",
                (token, username, expires),
            )
            db.commit()
    log.info(f"New user registered: {username} <{email}>")
    if _email_configured():
        try:
            _send_verification_email(email, username, token)
        except Exception as exc:
            log.error(f"Failed to send verification email to {email}: {exc}")
        return {"ok": True, "username": username, "verify_email": True}
    else:
        # No SMTP configured — auto-verify and auto-login
        with write_lock:
            with get_db() as db:
                db.execute("UPDATE users SET email_verified=1 WHERE username=?", (username,))
                db.commit()
        request.session["auth_user"] = username
        request.session["auth_role"] = "viewer"
        return {"ok": True, "username": username, "role": "viewer", "verify_email": False}


@router.get("/api/auth/verify-email")
def verify_email(token: str):
    now = datetime.now(timezone.utc).isoformat()
    with write_lock:
        with get_db() as db:
            row = db.execute(
                "SELECT username, expires_at FROM email_verifications WHERE token=?", (token,)
            ).fetchone()
            if not row:
                raise HTTPException(400, "Invalid or already used verification link")
            if row["expires_at"] < now:
                db.execute("DELETE FROM email_verifications WHERE token=?", (token,))
                db.commit()
                raise HTTPException(400, "Verification link has expired. Please register again.")
            db.execute("UPDATE users SET email_verified=1 WHERE username=?", (row["username"],))
            db.execute("DELETE FROM email_verifications WHERE token=?", (token,))
            db.commit()
    log.info(f"Email verified for user: {row['username']}")
    return {"ok": True, "username": row["username"]}


@router.post("/api/auth/change-password")
def auth_change_password(body: ChangePasswordIn, request: Request):
    if not is_authenticated(request):
        raise HTTPException(401, "Authentication required")

    if not body.new_password or len(body.new_password) < 8:
        raise HTTPException(400, "New password must be at least 8 characters")
    if body.new_password != body.confirm_password:
        raise HTTPException(400, "Password confirmation does not match")

    username = request.session.get("auth_user") or _current_auth_username()
    if not validate_credentials(username, body.current_password):
        raise HTTPException(401, "Current password is incorrect")

    user_row = _get_user(username)
    if user_row:
        salt = secrets.token_bytes(16)
        salt_b64 = base64.b64encode(salt).decode("ascii")
        hash_b64 = _pbkdf2_hash(body.new_password, salt_b64)
        with write_lock:
            with get_db() as db:
                db.execute(
                    "UPDATE users SET password_salt=?, password_hash=?, updated_at=? WHERE username=?",
                    (salt_b64, hash_b64, now_iso(), username),
                )
                db.commit()
    else:
        _set_hashed_password(username, body.new_password)
    return {"ok": True}


@router.get("/api/auth/status")
def auth_status(request: Request):
    enabled = auth_enabled()
    authenticated = is_authenticated(request)
    return {
        "enabled": enabled,
        "authenticated": authenticated,
        "user": request.session.get("auth_user") if authenticated else None,
        "role": current_role(request) if authenticated else None,
    }


@router.get("/api/auth/role")
def auth_role(request: Request):
    if not is_authenticated(request):
        raise HTTPException(401, "Authentication required")
    return {"role": current_role(request)}


@router.get("/api/users")
def list_users(request: Request):
    require_admin(request)
    with get_db() as db:
        rows = db.execute(
            "SELECT username, role, active, created_at, updated_at FROM users ORDER BY username"
        ).fetchall()
    return [dict(r) for r in rows]


@router.post("/api/users")
def create_user(body: UserCreateIn, request: Request):
    require_admin(request)
    role = _sanitize_role(body.role)
    username = (body.username or "").strip()
    if not username:
        raise HTTPException(400, "Username is required")
    if role not in VALID_ROLES:
        raise HTTPException(400, "Invalid role")

    with get_db() as db:
        exists = db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
    if exists:
        raise HTTPException(409, "User already exists")

    _create_user_record(username, body.password, role)
    return {"ok": True, "username": username, "role": role}


@router.patch("/api/users/{username}")
def update_user_role(username: str, body: UserRolePatchIn, request: Request):
    require_admin(request)
    # Only super_admin can grant/revoke super_admin role
    role = _sanitize_role(body.role)
    if role == "super_admin" and not is_super_admin(request):
        raise HTTPException(403, "Only super admins can grant super_admin role")
    active = 1 if (True if body.active is None else body.active) else 0

    with write_lock:
        with get_db() as db:
            user = db.execute("SELECT username, role, active FROM users WHERE username=?", (username,)).fetchone()
            if not user:
                raise HTTPException(404, "User not found")

            # Guard: ensure at least one active super_admin remains
            if user["role"] == "super_admin" and role != "super_admin":
                admins = db.execute(
                    "SELECT COUNT(*) FROM users WHERE role='super_admin' AND active=1"
                ).fetchone()[0]
                if admins <= 1 and int(user["active"] or 0) == 1:
                    raise HTTPException(400, "At least one active super admin is required")
            if user["role"] == "super_admin" and active == 0:
                admins = db.execute(
                    "SELECT COUNT(*) FROM users WHERE role='super_admin' AND active=1"
                ).fetchone()[0]
                if admins <= 1:
                    raise HTTPException(400, "At least one active super admin is required")

            db.execute(
                "UPDATE users SET role=?, active=?, updated_at=? WHERE username=?",
                (role, active, now_iso(), username),
            )
            db.commit()

    # Keep session role fresh if current user changed themselves.
    if request.session.get("auth_user") == username:
        request.session["auth_role"] = role
    return {"ok": True, "username": username, "role": role, "active": bool(active)}

@router.patch("/api/sites/{site_id}")
def update_site(site_id: str, body: SitePatch, request: Request):
    with write_lock:
        with get_db() as db:
            row = db.execute("SELECT id, owner FROM sites WHERE id=?", (site_id,)).fetchone()
            if not row:
                raise HTTPException(404, "Site not found")
            if not is_super_admin(request) and row["owner"] != current_user(request):
                raise HTTPException(403, "Not authorised to modify this site")
            updates = {}
            if body.name is not None: updates["name"] = body.name.strip()
            if body.group_name is not None: updates["group_name"] = body.group_name.strip()
            if body.max_pages is not None: updates["max_pages"] = max(1, min(body.max_pages, 20))
            if body.scan_interval is not None: updates["scan_interval"] = max(60, body.scan_interval)
            if body.rule_include_keywords is not None:
                updates["rule_include_keywords"] = body.rule_include_keywords.strip()
            if body.rule_exclude_keywords is not None:
                updates["rule_exclude_keywords"] = body.rule_exclude_keywords.strip()
            if body.rule_min_duration is not None:
                updates["rule_min_duration"] = max(0, body.rule_min_duration)
            if body.scan_profile is not None:
                prof = body.scan_profile.strip().lower()
                updates["scan_profile"] = prof if prof in {"fast", "balanced", "deep"} else "balanced"
            if body.notify_enabled is not None:
                updates["notify_enabled"] = 1 if body.notify_enabled else 0
            if updates:
                set_clause = ", ".join(f"{k}=?" for k in updates)
                db.execute(f"UPDATE sites SET {set_clause} WHERE id=?",
                           (*updates.values(), site_id))
                db.commit()
            return dict(db.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone())

_LISTING_PATHS = {
    "videos", "scenes", "movies", "episodes", "clips",
    "content", "latest", "latest-updates", "top-rated",
    "most-popular", "categories", "models", "model", "pornstars",
    "tags", "channels", "studios", "networks", "search",
    "girls", "guys", "performers",
}

@router.post("/api/sites/fix-urls")
def fix_site_urls():
    fixed = []
    with write_lock:
        with get_db() as db:
            sites = [dict(r) for r in db.execute("SELECT id, url FROM sites")]
            for s in sites:
                url = s["url"]
                p = urlparse(url)
                if not p.path or p.path == "/" or p.path.endswith("/") or p.query:
                    continue
                last_seg = p.path.rstrip("/").split("/")[-1].lower()
                if last_seg not in _LISTING_PATHS:
                    new_url = url + "/"
                    try:
                        db.execute("UPDATE sites SET url=? WHERE id=?", (new_url, s["id"]))
                        fixed.append({"id": s["id"], "old": url, "new": new_url})
                        log.info(f"Fixed URL: {url} → {new_url}")
                    except Exception as e:
                        log.warning(f"Could not fix {url}: {e}")
            db.commit()
    return {"fixed": fixed}

@router.delete("/api/sites/{site_id}")
def remove_site(site_id: str, request: Request):
    with write_lock:
        with get_db() as db:
            row = db.execute("SELECT owner FROM sites WHERE id=?", (site_id,)).fetchone()
            if not row:
                raise HTTPException(404, "Site not found")
            if not is_super_admin(request) and row["owner"] != current_user(request):
                raise HTTPException(403, "Not authorised to delete this site")
            db.execute("DELETE FROM sites WHERE id=?", (site_id,))
            db.execute("DELETE FROM videos WHERE site_id=?", (site_id,))
            # Auto-disable auto-scan when no sites remain
            remaining = db.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
            if remaining == 0:
                db.execute(
                    "INSERT INTO app_settings (key, value) VALUES ('autoscan_enabled', '0') "
                    "ON CONFLICT(key) DO UPDATE SET value='0'",
                )
            db.commit()
    cp = cookie_path(site_id)
    if cp.exists():
        cp.unlink()
    return {"ok": True}

@router.get("/api/videos")
def list_videos(request: Request,
                site_id: str | None = None,
                group_name: str | None = None,
                page: int = 1, per_page: int = 24,
                search: str = "", platform: str = "",
                released_after: str = "", released_before: str = "",
                duration_min: int | None = None, duration_max: int | None = None,
                include_archived: bool = False,
                include_ignored: bool = False,
                favorites_only: bool = False,
                unwatched_only: bool = False,
                archived_only: bool = False,
                ignored_only: bool = False):
    offset = (page - 1) * per_page
    with get_db() as db:
        filters = []
        params = []
        if not is_super_admin(request):
            filters.append("sites.owner=?")
            params.append(current_user(request) or "")
        if group_name is not None:
            if group_name == '__UNGROUPED__':
                filters.append("(sites.group_name IS NULL OR sites.group_name = '')")
            else:
                filters.append("sites.group_name=?"); params.append(group_name)
        if site_id:
            filters.append("videos.site_id=?"); params.append(site_id)
        if search:
            search_term = f"%{search}%"
            filters.append(
                "(videos.title LIKE ? OR videos.cast_names LIKE ? OR videos.url LIKE ? "
                "OR videos.platform LIKE ? OR sites.name LIKE ? OR sites.group_name LIKE ? )"
            )
            params.extend([search_term] * 6)
        if released_after:
            filters.append("videos.released_at >= ?"); params.append(released_after)
        if released_before:
            filters.append("videos.released_at <= ?"); params.append(released_before)
        if duration_min is not None:
            filters.append("videos.duration >= ?"); params.append(duration_min)
        if duration_max is not None:
            filters.append("videos.duration <= ?"); params.append(duration_max)
        if platform:
            filters.append("videos.platform=?"); params.append(platform)
        if not include_archived:
            filters.append("COALESCE(videos.is_archived, 0)=0")
        if not include_ignored:
            filters.append("COALESCE(videos.is_ignored, 0)=0")
        if favorites_only:
            filters.append("COALESCE(videos.is_favorite, 0)=1")
        if unwatched_only:
            filters.append("COALESCE(videos.is_watched, 0)=0")
        if archived_only:
            filters.append("COALESCE(videos.is_archived, 0)=1")
        if ignored_only:
            filters.append("COALESCE(videos.is_ignored, 0)=1")

        where = ("WHERE " + " AND ".join(filters)) if filters else ""
        order = """ORDER BY SUBSTR(COALESCE(videos.released_at, videos.found_at), 1, 19) DESC"""

        total = db.execute(
            f"SELECT COUNT(*) FROM videos LEFT JOIN sites ON videos.site_id = sites.id {where}",
            params).fetchone()[0]
        rows = db.execute(
            f"SELECT videos.* FROM videos LEFT JOIN sites ON videos.site_id = sites.id {where} {order} LIMIT ? OFFSET ?",
            params + [per_page, offset]).fetchall()

    return {
        "videos":      [dict(r) for r in rows],
        "total":       total,
        "page":        page,
        "per_page":    per_page,
        "total_pages": max(1, -(-total // per_page)),
    }

@router.post("/api/videos/bulk")
def bulk_video_action(body: BulkVideoActionIn):
    if not body.video_ids:
        return {"ok": True, "affected": 0}
    VALID_ACTIONS = {
        "favorite", "unfavorite", "archive", "unarchive",
        "ignore", "unignore", "mark_seen", "mark_watched", "mark_unwatched",
    }
    if body.action not in VALID_ACTIONS:
        raise HTTPException(400, f"Unknown action: {body.action}")

    placeholders = ",".join("?" * len(body.video_ids))
    with write_lock:
        with get_db() as db:
            if body.action == "favorite":
                db.execute(f"UPDATE videos SET is_favorite=1 WHERE id IN ({placeholders})", body.video_ids)
            elif body.action == "unfavorite":
                db.execute(f"UPDATE videos SET is_favorite=0 WHERE id IN ({placeholders})", body.video_ids)
            elif body.action == "archive":
                db.execute(f"UPDATE videos SET is_archived=1 WHERE id IN ({placeholders})", body.video_ids)
            elif body.action == "unarchive":
                db.execute(f"UPDATE videos SET is_archived=0 WHERE id IN ({placeholders})", body.video_ids)
            elif body.action == "ignore":
                db.execute(f"UPDATE videos SET is_ignored=1 WHERE id IN ({placeholders})", body.video_ids)
            elif body.action == "unignore":
                db.execute(f"UPDATE videos SET is_ignored=0 WHERE id IN ({placeholders})", body.video_ids)
            elif body.action == "mark_seen":
                db.execute(f"UPDATE videos SET is_new=0 WHERE id IN ({placeholders})", body.video_ids)
            elif body.action == "mark_watched":
                db.execute(
                    f"UPDATE videos SET is_watched=1, is_new=0, last_watched_at=? WHERE id IN ({placeholders})",
                    [now_iso()] + body.video_ids,
                )
            elif body.action == "mark_unwatched":
                db.execute(f"UPDATE videos SET is_watched=0, last_watched_at=NULL WHERE id IN ({placeholders})", body.video_ids)
            db.commit()
            affected = db.execute(
                f"SELECT COUNT(*) FROM videos WHERE id IN ({placeholders})", body.video_ids
            ).fetchone()[0]
    return {"ok": True, "affected": affected}


@router.post("/api/videos/mark-seen")
def mark_seen(body: MarkSeenIn):
    with write_lock:
        with get_db() as db:
            if body.site_id:
                db.execute("UPDATE videos SET is_new=0 WHERE site_id=?", (body.site_id,))
            else:
                db.execute("UPDATE videos SET is_new=0")
            db.commit()
    return {"ok": True}

@router.delete("/api/videos/junk")
def clear_junk_videos():
    with write_lock:
        with get_db() as db:
            result = db.execute(
                "DELETE FROM videos WHERE url LIKE 'blob:%' OR url LIKE 'data:%' "
                "OR url LIKE 'javascript:%' OR length(url) < 12"
            )
            deleted = result.rowcount
            db.commit()
            log.info(f"Cleaned {deleted} junk video(s) from DB")
    return {"deleted": deleted}

@router.delete("/api/videos")
def clear_videos(site_id: str | None = None):
    with write_lock:
        with get_db() as db:
            if site_id:
                db.execute("DELETE FROM videos WHERE site_id=?", (site_id,))
                db.execute("UPDATE sites SET last_scan=NULL WHERE id=?", (site_id,))
            else:
                db.execute("DELETE FROM videos")
                db.execute("UPDATE sites SET last_scan=NULL")
            db.commit()
    return {"ok": True}

# ── Background Sync Task Managers ─────────────────────────────────────────────

def _run_scan_all_sync():
    if sys.platform.startswith("win"):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        except Exception:
            pass
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(scan_all_sites(push_progress))
    finally:
        loop.close()

def _run_scan_one_sync(site: dict):
    if sys.platform.startswith("win"):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        except Exception:
            pass
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(scan_site(site, push_progress))
    finally:
        loop.close()

@router.post("/api/scan")
async def scan_all(background_tasks: BackgroundTasks, request: Request):
    if is_super_admin(request):
        background_tasks.add_task(_run_scan_all_sync)
    else:
        user = current_user(request) or ""
        with get_db() as db:
            owned = [dict(r) for r in db.execute("SELECT * FROM sites WHERE owner=?", (user,))]
        for site in owned:
            background_tasks.add_task(_run_scan_one_sync, site)
    return {"ok": True, "message": "Scan started"}

@router.post("/api/scan/fresh")
async def fresh_scan_all(background_tasks: BackgroundTasks, request: Request):
    with write_lock:
        with get_db() as db:
            if is_super_admin(request):
                deleted = db.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
                db.execute("DELETE FROM videos")
                db.execute("UPDATE sites SET last_scan=NULL")
            else:
                user = current_user(request) or ""
                deleted = db.execute(
                    "SELECT COUNT(*) FROM videos WHERE site_id IN (SELECT id FROM sites WHERE owner=?)",
                    (user,),
                ).fetchone()[0]
                db.execute(
                    "DELETE FROM videos WHERE site_id IN (SELECT id FROM sites WHERE owner=?)",
                    (user,),
                )
                db.execute("UPDATE sites SET last_scan=NULL WHERE owner=?", (user,))
            db.commit()
    if is_super_admin(request):
        background_tasks.add_task(_run_scan_all_sync)
    else:
        user = current_user(request) or ""
        with get_db() as db:
            owned = [dict(r) for r in db.execute("SELECT * FROM sites WHERE owner=?", (user,))]
        for site in owned:
            background_tasks.add_task(_run_scan_one_sync, site)
    return {"ok": True, "message": "Fresh scan started", "deleted": deleted}

@router.post("/api/scan/{site_id}")
async def scan_one(site_id: str, background_tasks: BackgroundTasks, request: Request):
    with get_db() as db:
        site = db.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()
    if not site:
        raise HTTPException(404, "Site not found")
    if not is_super_admin(request) and site["owner"] != current_user(request):
        raise HTTPException(403, "Not authorised to scan this site")
    background_tasks.add_task(_run_scan_one_sync, dict(site))
    return {"ok": True, "message": f"Scanning {site['url']}"}

@router.get("/api/scan/automation")
def get_scan_automation_status():
    with get_db() as db:
        row = db.execute(
            "SELECT value FROM app_settings WHERE key='autoscan_enabled'"
        ).fetchone()
    enabled = (row["value"] == "1") if row else True
    return {"enabled": enabled}

@router.post("/api/scan/automation/toggle")
def set_scan_automation_status(body: AutomationToggleIn):
    with write_lock:
        with get_db() as db:
            db.execute(
                "INSERT INTO app_settings (key, value) VALUES ('autoscan_enabled', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("1" if body.enabled else "0",),
            )
            db.commit()
    return {"ok": True, "enabled": body.enabled}

@router.get("/api/scan/status")
def scan_status():
    with get_db() as db:
        rows = db.execute(
            "SELECT scan_log.*, sites.name as site_name, sites.url as site_url "
            "FROM scan_log LEFT JOIN sites ON scan_log.site_id = sites.id "
            "ORDER BY scanned_at DESC LIMIT 50"
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("/api/scan/health")
def scan_health(limit: int = 20):
    limit = max(5, min(limit, 100))
    with get_db() as db:
        overall = db.execute(
            "SELECT COUNT(*) as runs, "
            "AVG(CASE WHEN message LIKE 'ERROR:%' THEN 0.0 ELSE 1.0 END) as success_rate, "
            "AVG(found) as avg_found, AVG(added) as avg_added "
            "FROM (SELECT * FROM scan_log ORDER BY id DESC LIMIT ?)",
            (limit,),
        ).fetchone()

        per_site = [dict(r) for r in db.execute(
            "SELECT s.id as site_id, COALESCE(NULLIF(s.name,''), s.url) as site_name, "
            "COUNT(l.id) as runs, "
            "ROUND(AVG(CASE WHEN l.message LIKE 'ERROR:%' THEN 0.0 ELSE 1.0 END), 3) as success_rate, "
            "ROUND(AVG(l.found), 2) as avg_found, "
            "ROUND(AVG(l.added), 2) as avg_added, "
            "MAX(l.scanned_at) as last_scan "
            "FROM sites s "
            "LEFT JOIN (SELECT * FROM scan_log ORDER BY id DESC LIMIT ?) l ON l.site_id=s.id "
            "GROUP BY s.id ORDER BY runs DESC, last_scan DESC",
            (limit * 10,),
        ).fetchall()]

    anomalies = []
    for s in per_site:
        runs = int(s.get("runs") or 0)
        avg_found = float(s.get("avg_found") or 0)
        success_rate = float(s.get("success_rate") or 0)
        if runs >= 3 and success_rate < 0.6:
            anomalies.append({"site_id": s["site_id"], "type": "high_error_rate", "detail": "Recent scans failing frequently"})
        if runs >= 3 and avg_found == 0:
            anomalies.append({"site_id": s["site_id"], "type": "zero_found", "detail": "Recent scans found no videos"})

    return {
        "window": limit,
        "overall": {
            "runs": int(overall["runs"] or 0),
            "success_rate": float(overall["success_rate"] or 0),
            "avg_found": float(overall["avg_found"] or 0),
            "avg_added": float(overall["avg_added"] or 0),
        },
        "sites": per_site,
        "anomalies": anomalies,
    }


@router.get("/api/notifications")
def get_notifications_settings(request: Request):
    if not is_authenticated(request):
        raise HTTPException(401, "Authentication required")
    return {
        "enabled": (_read_setting("notify_enabled") or "0") == "1",
        "webhook_url": _read_setting("notify_webhook_url") or "",
        "digest_minutes": int(_read_setting("notify_digest_minutes") or 0),
    }


@router.post("/api/notifications")
def set_notifications_settings(body: NotificationSettingsIn, request: Request):
    require_admin(request)
    digest = max(0, body.digest_minutes)
    _write_setting("notify_enabled", "1" if body.enabled else "0")
    _write_setting("notify_webhook_url", (body.webhook_url or "").strip())
    _write_setting("notify_digest_minutes", str(digest))
    return {"ok": True, "enabled": body.enabled, "digest_minutes": digest}


@router.post("/api/notifications/test")
def test_notifications(request: Request):
    require_admin(request)
    enabled = (_read_setting("notify_enabled") or "0") == "1"
    webhook = (_read_setting("notify_webhook_url") or "").strip()
    if not enabled or not webhook:
        raise HTTPException(400, "Notifications are disabled or webhook URL is missing")

    payload = {
        "text": "VideoWatch test notification from settings page.",
        "site": "VideoWatch",
        "found": 0,
        "added": 0,
        "time": now_iso(),
        "test": True,
    }
    try:
        r = httpx.post(webhook, json=payload, timeout=8.0)
        if r.status_code >= 400:
            raise HTTPException(502, f"Webhook returned {r.status_code}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Webhook request failed: {e}")

    return {"ok": True}


@router.patch("/api/videos/{video_id}/state")
def update_video_state(video_id: str, body: VideoStatePatch):
    updates = {}
    if body.is_favorite is not None:
        updates["is_favorite"] = 1 if body.is_favorite else 0
    if body.is_archived is not None:
        updates["is_archived"] = 1 if body.is_archived else 0
    if body.is_ignored is not None:
        updates["is_ignored"] = 1 if body.is_ignored else 0
    if body.is_watched is not None:
        updates["is_watched"] = 1 if body.is_watched else 0
        if body.is_watched:
            updates["last_watched_at"] = now_iso()
            updates["is_new"] = 0
    if not updates:
        return {"ok": True}

    with write_lock:
        with get_db() as db:
            row = db.execute("SELECT id FROM videos WHERE id=?", (video_id,)).fetchone()
            if not row:
                raise HTTPException(404, "Video not found")
            set_clause = ", ".join(f"{k}=?" for k in updates)
            db.execute(f"UPDATE videos SET {set_clause} WHERE id=?", (*updates.values(), video_id))
            db.commit()
    return {"ok": True}


@router.get("/api/videos/duplicates")
def list_duplicate_candidates(limit: int = 50):
    limit = max(10, min(limit, 200))
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT site_id, LOWER(TRIM(title)) as title_key, COALESCE(released_at,'') as rel_key, "
            "COUNT(*) as count "
            "FROM videos "
            "WHERE title IS NOT NULL AND TRIM(title) != '' "
            "GROUP BY site_id, title_key, rel_key HAVING COUNT(*) > 1 "
            "ORDER BY count DESC LIMIT ?",
            (limit,),
        ).fetchall()]

        result = []
        for g in rows:
            vids = [dict(v) for v in db.execute(
                "SELECT id, site_id, title, url, found_at, released_at, is_new "
                "FROM videos WHERE site_id=? AND LOWER(TRIM(title))=? AND COALESCE(released_at,'')=? "
                "ORDER BY found_at DESC",
                (g["site_id"], g["title_key"], g["rel_key"]),
            ).fetchall()]
            result.append({
                "site_id": g["site_id"],
                "title_key": g["title_key"],
                "released_at": g["rel_key"] or None,
                "count": g["count"],
                "videos": vids,
            })
    return result


@router.post("/api/videos/duplicates/merge")
def merge_duplicate(body: MergeDuplicateIn, request: Request):
    require_admin(request)
    if body.keep_id == body.remove_id:
        raise HTTPException(400, "keep_id and remove_id must differ")

    with write_lock:
        with get_db() as db:
            keep = db.execute("SELECT * FROM videos WHERE id=?", (body.keep_id,)).fetchone()
            rem = db.execute("SELECT * FROM videos WHERE id=?", (body.remove_id,)).fetchone()
            if not keep or not rem:
                raise HTTPException(404, "Duplicate rows not found")

            db.execute(
                "UPDATE videos SET "
                "thumb = COALESCE(thumb, ?), "
                "embed_url = COALESCE(embed_url, ?), "
                "cast_names = COALESCE(cast_names, ?), "
                "duration = COALESCE(duration, ?), "
                "is_favorite = MAX(is_favorite, ?), "
                "is_archived = MIN(is_archived, ?), "
                "is_ignored = MIN(is_ignored, ?) "
                "WHERE id=?",
                (
                    rem["thumb"],
                    rem["embed_url"],
                    rem["cast_names"],
                    rem["duration"],
                    rem["is_favorite"],
                    rem["is_archived"],
                    rem["is_ignored"],
                    body.keep_id,
                ),
            )
            db.execute("DELETE FROM videos WHERE id=?", (body.remove_id,))
            db.commit()
    return {"ok": True}

@router.get("/api/scan/stream")
async def scan_stream(request: Request):
    """SSE endpoint — one queue per client, messages filtered by site ownership."""
    # Determine which site IDs this client is allowed to see events for.
    # super_admin / auth-disabled → None means no filter (see everything).
    if is_super_admin(request):
        allowed_sites = None
    else:
        user = current_user(request) or ""
        with get_db() as db:
            allowed_sites = {
                r[0] for r in db.execute(
                    "SELECT id FROM sites WHERE owner=?", (user,)
                ).fetchall()
            }

    queue: asyncio.Queue = asyncio.Queue()
    _sse_subscribers.append(queue)

    async def event_gen():
        try:
            yield "data: connected\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=25)
                    # Filter: only forward events belonging to this user's sites.
                    if allowed_sites is not None:
                        parts = msg.split('|')
                        site_id = parts[1] if len(parts) >= 2 else ''
                        if site_id and site_id not in allowed_sites:
                            continue
                    yield f"data: {msg}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"   # keep-alive
        finally:
            try:
                _sse_subscribers.remove(queue)
            except ValueError:
                pass

    return StreamingResponse(event_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})

@router.get("/api/stats")
def stats(request: Request):
    with get_db() as db:
        if is_super_admin(request):
            ow, op = "", []
        else:
            ow, op = "AND sites.owner=?", [current_user(request) or ""]
        vj = f"FROM videos LEFT JOIN sites ON videos.site_id=sites.id WHERE 1=1 {ow}"
        total     = db.execute(f"SELECT COUNT(*) {vj}", op).fetchone()[0]
        new       = db.execute(f"SELECT COUNT(*) {vj} AND is_new=1", op).fetchone()[0]
        favorites = db.execute(f"SELECT COUNT(*) {vj} AND COALESCE(is_favorite,0)=1", op).fetchone()[0]
        archived  = db.execute(f"SELECT COUNT(*) {vj} AND COALESCE(is_archived,0)=1", op).fetchone()[0]
        ignored   = db.execute(f"SELECT COUNT(*) {vj} AND COALESCE(is_ignored,0)=1", op).fetchone()[0]
        if is_super_admin(request):
            sites = db.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
            scans = db.execute("SELECT COUNT(*) FROM scan_log").fetchone()[0]
            last  = db.execute("SELECT MAX(scanned_at) FROM scan_log").fetchone()[0]
            platforms = [dict(r) for r in db.execute(
                "SELECT platform, COUNT(*) as count FROM videos GROUP BY platform ORDER BY count DESC"
            ).fetchall()]
            site_list = [dict(r) for r in db.execute(
                "SELECT id, name, url, group_name FROM sites ORDER BY group_name, name"
            ).fetchall()]
        else:
            user = current_user(request) or ""
            sites = db.execute("SELECT COUNT(*) FROM sites WHERE owner=?", (user,)).fetchone()[0]
            scans = db.execute(
                "SELECT COUNT(*) FROM scan_log LEFT JOIN sites ON scan_log.site_id=sites.id WHERE sites.owner=?",
                (user,),
            ).fetchone()[0]
            last = db.execute(
                "SELECT MAX(scan_log.scanned_at) FROM scan_log LEFT JOIN sites ON scan_log.site_id=sites.id WHERE sites.owner=?",
                (user,),
            ).fetchone()[0]
            platforms = [dict(r) for r in db.execute(
                "SELECT videos.platform, COUNT(*) as count FROM videos LEFT JOIN sites ON videos.site_id=sites.id "
                "WHERE sites.owner=? GROUP BY videos.platform ORDER BY count DESC",
                (user,),
            ).fetchall()]
            site_list = [dict(r) for r in db.execute(
                "SELECT id, name, url, group_name FROM sites WHERE owner=? ORDER BY group_name, name",
                (user,),
            ).fetchall()]
    return {"total": total, "new": new, "sites": sites,
            "scans": scans, "last_scan": last,
            "favorites": favorites, "archived": archived, "ignored": ignored,
            "platforms": platforms, "site_list": site_list}

@router.get("/api/logs", response_class=HTMLResponse)
def get_logs(lines: int = 300):
    """Tail the server log file in the browser."""
    log_file = Path(__file__).parent / "videowatch.log"
    try:
        text = log_file.read_text(encoding="utf-8", errors="replace")
        tail = "\n".join(text.splitlines()[-lines:])
    except Exception as e:
        tail = f"Log file not found: {e}\n\nMake sure the server has been restarted after the latest update."
    safe = tail.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return HTMLResponse(f"""<!doctype html><html><head><meta charset=utf-8>
<title>VideoWatch Server Log</title>
<style>body{{background:#0d0d1a;color:#7ecb9f;font-family:monospace;font-size:13px;padding:1rem;margin:0}}
pre{{white-space:pre-wrap;word-break:break-all}}
.ts{{color:#555}} .err{{color:#f87171}} .warn{{color:#fbbf24}}</style></head>
<body><pre id=log>{safe}</pre>
<script>
// Auto-refresh every 4 seconds, keep scroll at bottom
function colorize(){{
  document.getElementById('log').innerHTML = document.getElementById('log').textContent
    .split('\\n').map(l=>{{
      if(l.includes(' ERROR ')||l.includes(' CRITICAL ')) return `<span class=err>${{l}}</span>`;
      if(l.includes(' WARNING ')) return `<span class=warn>${{l}}</span>`;
      const m=l.match(/^(\\d{{4}}-\\d{{2}}-\\d{{2}} \\d{{2}}:\\d{{2}}:\\d{{2}})/);
      return m?`<span class=ts>${{m[1]}}</span>${{l.slice(m[1].length)}}`:l;
    }}).join('\\n');
}}
colorize();
window.scrollTo(0,document.body.scrollHeight);
setInterval(()=>fetch(location.href+'?lines={lines}').then(r=>r.text()).then(h=>{{
  const parser=new DOMParser();
  const doc=parser.parseFromString(h,'text/html');
  document.getElementById('log').textContent=doc.getElementById('log').textContent;
  colorize();
  window.scrollTo(0,document.body.scrollHeight);
}}),4000);
</script></body></html>""")


@router.get("/api/health")
def health():
    healthy = True
    db_status = "ok"
    try:
        with get_db() as db:
            db.execute("SELECT 1")
    except Exception as exc:
        healthy = False
        db_status = str(exc)
    return JSONResponse(
        content={
            "status": "ok" if healthy else "error",
            "db_path": str(DB_PATH),
            "db_status": db_status,
            "host": os.environ.get("HOST", "0.0.0.0"),
            "time": now_iso(),
        },
        status_code=status.HTTP_200_OK if healthy else status.HTTP_500_INTERNAL_SERVER_ERROR,
    )

# ── Secured Thumbnail Proxy ───────────────────────────────────────────────────

@router.get("/api/thumb")
async def thumb_proxy(url: str = Query(...)):
    """
    Proxy + cache remote thumbnails locally.
    """
    # Whitelist check removed – allow any URL (now with safety checks)
    cache_key = hashlib.md5(url.encode()).hexdigest()
    cache_file = THUMBS_DIR / cache_key

    if cache_file.exists():
        data = cache_file.read_bytes()
        return Response(content=data, media_type="image/jpeg")

    # ---- Security checks -------------------------------------------------
    parsed = urlparse(url)

    # Allow only http/https schemes
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="Invalid URL scheme")

    # Disallow private IP addresses
    try:
        host_ip = ipaddress.ip_address(parsed.hostname)
        if host_ip.is_private:
            raise HTTPException(status_code=400, detail="Private address not allowed")
    except ValueError:
        # hostname is not an IP address – proceed
        pass

    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/126.0.0.0"
                ),
                "Referer": parsed.scheme + "://" + parsed.netloc + "/",
            }
            r = await client.get(url, headers=headers)

            # Propagate non‑200 status codes
            if r.status_code != 200:
                raise HTTPException(status_code=r.status_code, detail="Failed to fetch thumbnail")

            # Verify content type is an image
            content_type = r.headers.get("content-type", "")
            if not content_type.startswith("image/"):
                raise HTTPException(status_code=400, detail="URL does not point to an image")

            # Size limit: 5 MiB
            max_bytes = 5 * 1024 * 1024
            if int(r.headers.get("content-length", "0")) > max_bytes:
                raise HTTPException(status_code=400, detail="Image too large")
            if len(r.content) > max_bytes:
                raise HTTPException(status_code=400, detail="Image too large")

            cache_file.write_bytes(r.content)
            return Response(content=r.content, media_type=content_type or "image/jpeg")
    except HTTPException as he:
        # Forward known client errors
        raise he
    except Exception as e:
        log.warning(f"Thumb proxy failed for {url}: {e}")

    return Response(status_code=404)

async def iterfile(file_path: Path, start: int = 0, end: int = None):
    """Yield file chunks for streaming, optionally from start to end bytes."""
    with open(file_path, "rb") as f:
        f.seek(start)
        while True:
            chunk_size = 8192
            data = f.read(chunk_size if end is None else min(chunk_size, end - f.tell()))
            if not data:
                break
            yield data

@router.get("/api/video/{video_id}")
async def get_video(video_id: str, range: str = None):
    """Stream a video file with support for HTTP Range requests."""
    with get_db() as db:
        row = db.execute("SELECT local_file FROM videos WHERE id=?", (video_id,)).fetchone()
    file_path = VIDEOS_DIR / (row["local_file"] if row and row["local_file"] else video_id)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Video not found")
    file_size = file_path.stat().st_size
    content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    if range:
        # Expected format: bytes=start-end
        try:
            _, range_spec = range.split("=")
            start_str, end_str = range_spec.split("-")
            start = int(start_str) if start_str else 0
            end = int(end_str) if end_str else file_size - 1
        except Exception:
            start = 0
            end = file_size - 1
        length = end - start + 1
        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
        }
        return StreamingResponse(iterfile(file_path, start, end + 1), status_code=206, media_type=content_type, headers=headers)
    else:
        headers = {"Accept-Ranges": "bytes"}
        return StreamingResponse(iterfile(file_path), media_type=content_type, headers=headers)


@router.get("/api/video-resolve")
async def resolve_video_source(url: str = Query(...), debug: bool = False):
    """Try to resolve a page URL to a directly playable media URL."""
    resolved = await _resolve_video_source_impl(url)
    payload = {
        "resolved_url": resolved.get("resolved_url"),
        "kind": resolved.get("kind", "none"),
        "reason": resolved.get("reason", "resolver error"),
    }
    if debug:
        payload["diagnostics"] = resolved.get("diagnostics") or {}
        payload["diagnostics_summary"] = _format_resolver_diagnostics(resolved.get("diagnostics"))
    return payload


@router.post("/api/videos/{video_id}/download")
async def download_video(video_id: str):
    with get_db() as db:
        video = db.execute(
            "SELECT id, title, url, embed_url, local_file FROM videos WHERE id=?",
            (video_id,),
        ).fetchone()
    if not video:
        raise HTTPException(404, "Video not found")

    existing_file = video["local_file"] if video["local_file"] else None
    if existing_file and (VIDEOS_DIR / existing_file).exists():
        return {
            "ok": True,
            "status": "downloaded",
            "cached": True,
            "local_url": f"/api/video/{video_id}",
        }

    resolution_candidates = _build_resolution_candidates(video["url"], video["embed_url"])
    if not resolution_candidates:
        raise HTTPException(409, "Video has no source URL to resolve")

    resolved = None
    last_failed = None
    for candidate_url in resolution_candidates:
        attempt = await _resolve_video_source_impl(candidate_url, video["title"])
        if attempt.get("resolved_url"):
            resolved = attempt
            break
        last_failed = attempt
        diagnostics = attempt.get("diagnostics") or {}
        status_code = diagnostics.get("http_status")
        if status_code and status_code != 404:
            break

    resolved = resolved or last_failed or {
        "resolved_url": None,
        "kind": "none",
        "reason": "no playable media discovered",
        "diagnostics": None,
    }
    _update_video_download_metadata(
        video_id,
        resolved_media_url=resolved.get("resolved_url"),
        resolved_kind=resolved.get("kind"),
        resolved_at=now_iso(),
        download_status="resolving" if resolved.get("resolved_url") else "failed",
        download_error=None if resolved.get("resolved_url") else (
            f"{resolved.get('reason')}. {_format_resolver_diagnostics(resolved.get('diagnostics'))}".strip()
        ),
    )

    if not resolved.get("resolved_url"):
        message = resolved.get("reason") or "No downloadable media found"
        summary = _format_resolver_diagnostics(resolved.get("diagnostics"))
        if summary:
            message = f"{message}. {summary}"
        raise HTTPException(409, message)
    if resolved.get("kind") != "direct":
        _update_video_download_metadata(video_id, download_status="failed", download_error="Only direct file downloads are supported right now")
        raise HTTPException(409, "Only direct file downloads are supported right now")

    try:
        download = await _download_media_file(video_id, resolved["resolved_url"], resolved.get("headers") or _browser_headers(resolution_candidates[0]))
    except HTTPException as exc:
        _update_video_download_metadata(video_id, download_status="failed", download_error=str(exc.detail))
        raise
    except Exception as exc:
        _update_video_download_metadata(video_id, download_status="failed", download_error=str(exc))
        raise HTTPException(502, f"Download failed: {exc}")

    _update_video_download_metadata(
        video_id,
        local_file=download["filename"],
        download_status="downloaded",
        download_error=None,
    )
    return {
        "ok": True,
        "status": "downloaded",
        "cached": False,
        "local_url": f"/api/video/{video_id}",
        "resolved_url": resolved["resolved_url"],
    }


_THUMB_ATTRS = ("src", "data-src", "data-lazy-src", "data-lazy",
                "data-original", "data-image", "data-background",
                "data-thumb", "data-poster", "data-url")

_SKIP_WORDS = re.compile(
    r'\b(login|register|signup|sign.up|contact|about|faq|privacy|terms'
    r'|cookie|sitemap|advertis|newsletter|search|category|categories'
    r'|tag|tags|channel|channels|model|models|studio|studios)\b',
    re.I
)


def _broad_scrape(html: str, base_url: str, seen: set, limit: int = 24) -> list[dict]:
    """Extract content-looking links with thumbnails from raw HTML."""
    from bs4 import BeautifulSoup as _BS
    soup = _BS(html, "html.parser")
    base_host = urlparse(base_url).netloc.lower()
    results: list[dict] = []

    for a in soup.find_all("a", href=True):
        if len(results) >= limit:
            break
        href = (a.get("href") or "").strip()
        if not href:
            continue
        try:
            full = urljoin(base_url, href)
        except Exception:
            continue
        p2 = urlparse(full)
        if p2.scheme not in {"http", "https"}:
            continue
        if p2.netloc.lower() != base_host:
            continue
        path = p2.path.rstrip("/")
        segments = [s for s in path.split("/") if s]
        # Allow single-segment VK video URLs (e.g. /video-123456_789)
        is_vk_video = bool(re.search(r'/video-?\d+_\d+', path, re.I))
        if len(segments) < 2 and not is_vk_video:
            continue
        slug = segments[-1] if segments else path
        if not is_vk_video and (len(slug) < 4 or re.search(r'^\d+$', slug)):
            continue
        if _SKIP_WORDS.search(full):
            continue
        norm = full.split("?")[0].split("#")[0]
        if norm in seen:
            continue
        seen.add(norm)

        title = a.get_text(" ", strip=True) or slug.replace("-", " ").replace("_", " ")
        thumb = None
        for img in a.find_all("img"):
            for attr in _THUMB_ATTRS:
                src = img.get(attr, "").strip()
                if src and not src.startswith("data:"):
                    try:
                        thumb = urljoin(base_url, src)
                    except Exception:
                        pass
                    break
            if thumb:
                break
        if not thumb:
            for el in a.find_all(True):
                style = el.get("style", "")
                m = re.search(r'background(?:-image)?\s*:\s*url\(["\']?([^"\')\s]+)["\']?\)', style)
                if m:
                    src = m.group(1).strip()
                    if src and not src.startswith("data:"):
                        try:
                            thumb = urljoin(base_url, src)
                            break
                        except Exception:
                            pass

        results.append({"url": full, "title": title, "thumb": thumb,
                        "embed_url": None, "platform": "direct",
                        "released_at": None, "cast_names": None, "duration": None})
    return results


async def _preview_fetch_html(page_url_str: str, site_id: str, use_playwright: bool) -> str:
    """Fetch a single page via httpx, with optional Playwright fallback."""
    html = ""
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            r = await client.get(page_url_str, headers=_browser_headers(page_url_str))
            if r.status_code == 200:
                html = r.text
    except Exception as e:
        log.warning(f"Preview httpx failed for {page_url_str}: {e}")

    if use_playwright:
        try:
            from scraper import _make_context
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser, context = await _make_context(p, site_id)
                try:
                    pg = await context.new_page()
                    try:
                        await pg.goto(page_url_str, timeout=20000, wait_until="domcontentloaded")
                        await pg.wait_for_timeout(3000)
                        html = await pg.content()
                    finally:
                        await pg.close()
                finally:
                    await context.close()
                    await browser.close()
        except Exception as e:
            log.warning(f"Preview Playwright failed for {page_url_str}: {e}")

    return html


def _filter_by_keywords(videos: list[dict], keywords: str, fallback: bool = True) -> list[dict]:
    """Return videos whose title/url match any keyword. Falls back to all if none match."""
    if not keywords or not keywords.strip():
        return videos
    terms = [t.strip().lower() for t in re.split(r'[,\s]+', keywords.strip()) if len(t.strip()) >= 2]
    if not terms:
        return videos

    def score(v: dict) -> int:
        text = ((v.get("title") or "") + " " + (v.get("url") or "")).lower()
        return sum(1 for t in terms if t in text)

    matched = [v for v in videos if score(v) > 0]
    matched.sort(key=score, reverse=True)
    return matched if matched else (videos if fallback else [])


@router.get("/api/sites/preview")
async def preview_site(url: str = Query(...), max_pages: int = Query(1, ge=1, le=5), keywords: str = Query("")):
    """Scan up to max_pages pages of a URL and return found videos without saving to DB."""
    url = url.strip()
    if not url.startswith("http"):
        raise HTTPException(400, "URL must start with http:// or https://")
    if urlparse(url).scheme not in {"http", "https"}:
        raise HTTPException(400, "Invalid URL scheme")

    max_pages = max(1, min(max_pages, 5))
    site_id = short_id(url)

    async def _run_preview():
        from scraper import _is_youtube_channel_url, _scrape_youtube_channel, scrape_videos, page_url as _page_url

        if _is_youtube_channel_url(url):
            try:
                yt_vids = await asyncio.to_thread(_scrape_youtube_channel, url, max_pages * 12)
                return yt_vids[:(max_pages * 12)], ""
            except Exception as e:
                return [], str(e)

        all_vids: list[dict] = []
        seen_urls: set = set()
        pages_fetched = 0
        use_playwright = False  # start with httpx; switch to Playwright if needed

        for pg_num in range(1, max_pages + 1):
            pg_url = _page_url(url, pg_num)

            html = await _preview_fetch_html(pg_url, site_id, use_playwright=False)

            strict = scrape_videos(html, pg_url) if html else []
            if not strict and not use_playwright:
                # First page needs Playwright — use it for all subsequent pages too
                use_playwright = True
                html = await _preview_fetch_html(pg_url, site_id, use_playwright=True)
                strict = scrape_videos(html, pg_url) if html else []

            if strict:
                for v in strict:
                    norm = (v.get("url") or "").split("?")[0]
                    if norm not in seen_urls:
                        seen_urls.add(norm)
                        all_vids.append(v)
            else:
                broad = _broad_scrape(html or "", pg_url, seen_urls)
                all_vids.extend(broad)

            pages_fetched += 1

            # Stop early if a paginated URL looks the same as previous
            if pg_num > 1 and not strict and not _broad_scrape(html or "", pg_url, set()):
                break

            if pg_num < max_pages:
                await asyncio.sleep(0.5)

        if not all_vids:
            hint = "Page fetched but no recognisable video or content links found. Try a listing page URL (e.g. /videos, /scenes, /clips)."
            return [], hint

        return all_vids[:(max_pages * 24)], ""

    found_videos: list[dict] = []
    error_detail: str = ""
    try:
        result = await asyncio.wait_for(_run_preview(), timeout=max_pages * 40)
        found_videos, error_detail = result
    except asyncio.TimeoutError:
        error_detail = "Preview timed out. The site may be slow or blocking automated access."
        log.warning(f"Preview timed out for {url}")
    except Exception as e:
        error_detail = str(e)
        log.warning(f"Preview error for {url}: {e}")

    if found_videos and keywords:
        found_videos = _filter_by_keywords(found_videos, keywords)

    return {
        "url": url,
        "count": len(found_videos),
        "videos": found_videos,
        "hint": error_detail,
    }


@router.get("/api/sites/detect-listing")
async def detect_listing_url(url: str = Query(...)):
    """Fetch a homepage and find listing page links (videos, scenes, etc.)."""
    _LISTING_SEGS = {
        "videos", "scenes", "movies", "episodes", "clips", "content",
        "latest", "latest-updates", "top-rated", "most-popular",
        "categories", "models", "model", "pornstars", "tags", "channels",
        "studios", "networks", "search", "girls", "guys", "performers",
        "gallery", "galleries", "updates",
    }
    url = url.strip()
    if not url.startswith("http"):
        raise HTTPException(400, "URL must start with http")
    try:
        html = None
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True,
                                         headers={"User-Agent": "Mozilla/5.0"}) as client:
                r = await client.get(url)
                html = r.text
        except Exception:
            pass
        if not html or len(html) < 1000:
            html = await _preview_fetch_html(url)
        if not html:
            return {"suggestions": []}
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        base = urlparse(url)
        base_root = f"{base.scheme}://{base.netloc}"
        seen = set()
        suggestions = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith("/"):
                href = base_root + href
            elif not href.startswith("http"):
                continue
            p = urlparse(href)
            if p.netloc != base.netloc:
                continue
            segs = [s.lower() for s in p.path.strip("/").split("/") if s]
            if not segs:
                continue
            seg = segs[0]
            if seg in _LISTING_SEGS and href not in seen:
                seen.add(href)
                label = (a.get_text(strip=True) or seg).strip()[:40]
                suggestions.append({"url": href, "label": label or seg})
            if len(suggestions) >= 10:
                break
        return {"suggestions": suggestions}
    except Exception as e:
        log.warning(f"detect-listing error: {e}")
        return {"suggestions": []}


@router.get("/api/sites/discover")
async def discover_sites(q: str = Query(...)):
    """Search DuckDuckGo for websites matching the given keywords and return URL suggestions."""
    query = q.strip()
    if not query:
        raise HTTPException(400, "Search query is required")

    from urllib.parse import quote as url_quote
    search_url = f"https://lite.duckduckgo.com/lite/?q={url_quote(query + ' video site')}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml",
    }

    results: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(search_url, headers=headers)
            if r.status_code == 200:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(r.text, "html.parser")
                seen_hosts: set[str] = set()
                skip = {
                    "google.com", "facebook.com", "twitter.com", "wikipedia.org",
                    "reddit.com", "instagram.com", "linkedin.com", "duckduckgo.com",
                    "bing.com", "amazon.com", "youtube.com", "tiktok.com",
                }
                # DDG Lite wraps links as //duckduckgo.com/l/?uddg=<encoded-url>
                from urllib.parse import unquote, parse_qs
                for a in soup.find_all("a", href=True):
                    raw_href = (a.get("href") or "").strip()
                    # Extract real URL from uddg param
                    if "uddg=" in raw_href:
                        qs_part = urlparse("https:" + raw_href).query if raw_href.startswith("//") else urlparse(raw_href).query
                        uddg = parse_qs(qs_part).get("uddg", [None])[0]
                        href = unquote(uddg) if uddg else ""
                    else:
                        href = raw_href
                    if not href.startswith("http"):
                        continue
                    p = urlparse(href)
                    host = p.netloc.lower()
                    if not host or host in seen_hosts:
                        continue
                    if any(host == s or host.endswith("." + s) for s in skip):
                        continue
                    seen_hosts.add(host)
                    site_url = f"{p.scheme}://{p.netloc}/"
                    title = a.get_text(strip=True) or host
                    if len(title) < 3 or len(title) > 120:
                        continue
                    results.append({"url": site_url, "title": title, "host": host})
                    if len(results) >= 8:
                        break
            else:
                log.warning(f"DuckDuckGo returned status {r.status_code}")
    except Exception as e:
        log.warning(f"Site discovery search failed: {e}")

    return {"query": query, "results": results}
