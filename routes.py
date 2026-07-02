import os
import sys
import csv
import io
import json
import hashlib
import base64
import logging
import asyncio
import hmac
import secrets
import re
import shutil
import threading
import queue
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
DOWNLOADS_DIR = Path(__file__).resolve().parent / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

# Directory for automated DB backups
BACKUPS_DIR = Path(__file__).resolve().parent / "backups"
BACKUPS_DIR.mkdir(exist_ok=True)
_BACKUP_KEEP = 7  # number of daily backups to retain
LEGACY_VIDEOS_DIR = Path(__file__).resolve().parent.parent / "videos"
import ipaddress
import time
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from db import get_db, write_lock, DB_PATH

# ── Rate limiting ─────────────────────────────────────────────────────────────
_rate_limit_store: dict[str, list[float]] = {}
_rate_limit_last_pruned: float = 0.0

def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

def _check_rate_limit(ip: str, max_attempts: int = 5, window_seconds: int = 300) -> None:
    global _rate_limit_last_pruned
    now = time.monotonic()
    # Prune stale IPs every 5 minutes to prevent unbounded memory growth
    if now - _rate_limit_last_pruned > 300:
        cutoff = now - max(window_seconds, 3600)
        _rate_limit_store.clear() if len(_rate_limit_store) > 5000 else None
        stale = [k for k, v in _rate_limit_store.items() if not any(t > cutoff for t in v)]
        for k in stale:
            del _rate_limit_store[k]
        _rate_limit_last_pruned = now
    hits = _rate_limit_store.get(ip, [])
    hits = [t for t in hits if now - t < window_seconds]
    hits.append(now)
    _rate_limit_store[ip] = hits
    if len(hits) > max_attempts:
        retry_after = int(window_seconds - (now - hits[0]))
        raise HTTPException(
            status_code=429,
            detail=f"Too many attempts. Please wait {retry_after} seconds before trying again.",
            headers={"Retry-After": str(retry_after)},
        )

# ── Email (Gmail SMTP) ────────────────────────────────────────────────────────
_SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
_SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
_SMTP_USER = os.environ.get("SMTP_USER", "")
_SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
_APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000")

def _email_configured() -> bool:
    return bool(_SMTP_USER and _SMTP_PASSWORD)

def _send_verification_email(to_address: str, username: str, token: str) -> None:
    verify_url = f"{_APP_BASE_URL}/verify-email?token={token}"
    html = f"""
    <div style="font-family:sans-serif;max-width:480px;margin:auto;padding:24px">
      <h2 style="color:#0f766e;margin-bottom:8px">Verify your VideoWatch account</h2>
      <p style="color:#374151">Hi <strong>{username}</strong>,</p>
      <p style="color:#374151">Click the button below to verify your email address and activate your account.</p>
      <a href="{verify_url}"
         style="display:inline-block;padding:12px 28px;background:#0f766e;color:#fff;
                text-decoration:none;border-radius:8px;font-weight:700;margin:16px 0;font-size:15px">
        Verify Email Address
      </a>
      <p style="color:#6b7280;font-size:13px;margin-top:24px">
        This link expires in <strong>24 hours</strong>.<br>
        If you didn't create a VideoWatch account, you can safely ignore this email.
      </p>
      <hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0"/>
      <p style="color:#9ca3af;font-size:12px">VideoWatch · Your personal video monitoring hub</p>
    </div>
    """
    text = (
        f"Hi {username},\n\n"
        f"Verify your VideoWatch account by visiting:\n{verify_url}\n\n"
        f"This link expires in 24 hours.\n\n"
        f"If you didn't register, ignore this email."
    )
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Verify your VideoWatch account"
    msg["From"] = f"VideoWatch <{_SMTP_USER}>"
    msg["To"] = to_address
    msg["Reply-To"] = _SMTP_USER
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(_SMTP_USER, _SMTP_PASSWORD)
        smtp.sendmail(_SMTP_USER, to_address, msg.as_string())


def _send_reset_email(to_address: str, username: str, token: str) -> None:
    reset_url = f"{_APP_BASE_URL}/reset-password?token={token}"
    html = f"""
    <div style="font-family:sans-serif;max-width:480px;margin:auto;padding:24px">
      <h2 style="color:#0f766e;margin-bottom:8px">Reset your password</h2>
      <p style="color:#374151">Hi <strong>{username}</strong>,</p>
      <p style="color:#374151">We received a request to reset your VideoWatch password. Click the button below to choose a new one.</p>
      <a href="{reset_url}"
         style="display:inline-block;padding:12px 28px;background:#0f766e;color:#fff;
                text-decoration:none;border-radius:8px;font-weight:700;margin:16px 0;font-size:15px">
        Reset Password
      </a>
      <p style="color:#6b7280;font-size:13px;margin-top:24px">
        This link expires in <strong>1 hour</strong>.<br>
        If you didn't request a password reset, you can safely ignore this email.
      </p>
      <hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0"/>
      <p style="color:#9ca3af;font-size:12px">VideoWatch · Your personal video monitoring hub</p>
    </div>
    """
    text = (
        f"Hi {username},\n\n"
        f"Reset your VideoWatch password by visiting:\n{reset_url}\n\n"
        f"This link expires in 1 hour.\n\n"
        f"If you didn't request this, ignore this email."
    )
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Reset your VideoWatch password"
    msg["From"] = f"VideoWatch <{_SMTP_USER}>"
    msg["To"] = to_address
    msg["Reply-To"] = _SMTP_USER
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(_SMTP_USER, _SMTP_PASSWORD)
        smtp.sendmail(_SMTP_USER, to_address, msg.as_string())

def send_weekly_digest():
    """Send each user a summary of new videos found in the last 7 days. Call on Monday."""
    if not _email_configured():
        return
    from datetime import timedelta
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    with get_db() as db:
        users = db.execute(
            "SELECT username, email FROM users WHERE email IS NOT NULL AND email_verified=1 AND notify_new_videos=1"
        ).fetchall()
        for user in users:
            username = user["email"] if user["email"] else None
            if not username:
                continue
            rows = db.execute(
                """SELECT v.title, v.url, v.thumb, s.name AS site_name
                   FROM videos v JOIN sites s ON s.id=v.site_id
                   WHERE s.owner=? AND v.found_at >= ? AND v.is_ignored=0
                   ORDER BY v.found_at DESC LIMIT 20""",
                (user["username"], since)
            ).fetchall()
            if not rows:
                continue
            count = db.execute(
                "SELECT COUNT(*) FROM videos v JOIN sites s ON s.id=v.site_id WHERE s.owner=? AND v.found_at >= ?",
                (user["username"], since)
            ).fetchone()[0]
            items_html = "".join(
                f'<tr><td style="padding:6px 0;border-bottom:1px solid #e2e8f0;">'
                f'<a href="{r["url"]}" style="color:#0f766e;font-weight:600;text-decoration:none;">{r["title"] or r["url"]}</a>'
                f'<span style="color:#94a3b8;font-size:.8em;margin-left:.5rem;">{r["site_name"] or ""}</span>'
                f'</td></tr>'
                for r in rows
            )
            more = f'<p style="color:#64748b;font-size:.85rem;">…and {count - len(rows)} more.</p>' if count > len(rows) else ""
            html = f"""
            <div style="font-family:sans-serif;max-width:560px;margin:0 auto;">
              <h2 style="color:#0f766e;">VideoWatch Weekly Digest</h2>
              <p>Here's what's new since last week, <strong>{user["username"]}</strong>:</p>
              <p style="font-size:1.1rem;font-weight:700;">{count} new video{"s" if count!=1 else ""} found</p>
              <table style="width:100%;border-collapse:collapse;">{items_html}</table>
              {more}
              <p style="margin-top:1.5rem;">
                <a href="{_APP_BASE_URL}" style="background:#0f766e;color:#fff;padding:10px 22px;border-radius:8px;text-decoration:none;font-weight:700;">
                  Open VideoWatch
                </a>
              </p>
              <p style="color:#94a3b8;font-size:.8rem;margin-top:2rem;">
                You're receiving this weekly digest because you have email notifications enabled.<br>
                <a href="{_APP_BASE_URL}/settings?tab=notifications" style="color:#94a3b8;">Unsubscribe</a>
              </p>
            </div>"""
            text = f"VideoWatch Weekly Digest\n\n{count} new video(s) found this week.\n\nOpen VideoWatch: {_APP_BASE_URL}\n"
            try:
                _send_email_simple(user["email"], "Your VideoWatch Weekly Digest", html, text)
                log.info(f"Weekly digest sent to {user['username']}")
            except Exception as e:
                log.warning(f"Weekly digest failed for {user['username']}: {e}")


def _send_email_simple(to_address: str, subject: str, html: str, text: str) -> None:
    """Fire-and-forget helper — call in a daemon thread."""
    if not _email_configured() or not to_address:
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"VideoWatch <{_SMTP_USER}>"
    msg["To"] = to_address
    msg["Reply-To"] = _SMTP_USER
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT) as smtp:
            smtp.ehlo(); smtp.starttls()
            smtp.login(_SMTP_USER, _SMTP_PASSWORD)
            smtp.sendmail(_SMTP_USER, to_address, msg.as_string())
    except Exception as exc:
        log.warning("Email send failed (%s): %s", subject, exc)


def _notify_password_changed(username: str, email: str, ip: str) -> None:
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html = f"""
    <div style="font-family:sans-serif;max-width:480px;margin:auto;padding:24px">
      <h2 style="color:#0f766e;margin-bottom:8px">Your password was changed</h2>
      <p style="color:#374151">Hi <strong>{username}</strong>,</p>
      <p style="color:#374151">Your VideoWatch account password was successfully changed.</p>
      <table style="font-size:13px;color:#374151;border-collapse:collapse;margin:12px 0">
        <tr><td style="padding:4px 12px 4px 0;color:#6b7280">When</td><td>{when}</td></tr>
        <tr><td style="padding:4px 12px 4px 0;color:#6b7280">IP address</td><td>{ip}</td></tr>
      </table>
      <p style="color:#374151;font-size:13px">If you didn't make this change, please <a href="{_APP_BASE_URL}/forgot-password" style="color:#0f766e">reset your password</a> immediately.</p>
      <hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0"/>
      <p style="color:#9ca3af;font-size:12px">VideoWatch · Your personal video monitoring hub</p>
    </div>"""
    text = (
        f"Hi {username},\n\nYour VideoWatch password was changed on {when} from IP {ip}.\n\n"
        f"If you didn't do this, reset your password at: {_APP_BASE_URL}/forgot-password\n"
    )
    threading.Thread(
        target=_send_email_simple,
        args=(email, "Your VideoWatch password was changed", html, text),
        daemon=True,
    ).start()


def _notify_new_login(username: str, email: str, ip: str) -> None:
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html = f"""
    <div style="font-family:sans-serif;max-width:480px;margin:auto;padding:24px">
      <h2 style="color:#0f766e;margin-bottom:8px">New sign-in to VideoWatch</h2>
      <p style="color:#374151">Hi <strong>{username}</strong>,</p>
      <p style="color:#374151">We noticed a new sign-in to your account.</p>
      <table style="font-size:13px;color:#374151;border-collapse:collapse;margin:12px 0">
        <tr><td style="padding:4px 12px 4px 0;color:#6b7280">When</td><td>{when}</td></tr>
        <tr><td style="padding:4px 12px 4px 0;color:#6b7280">IP address</td><td>{ip}</td></tr>
      </table>
      <p style="color:#374151;font-size:13px">If this wasn't you, please <a href="{_APP_BASE_URL}/forgot-password" style="color:#0f766e">reset your password</a> immediately.</p>
      <hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0"/>
      <p style="color:#9ca3af;font-size:12px">VideoWatch · Your personal video monitoring hub</p>
    </div>"""
    text = (
        f"Hi {username},\n\nNew sign-in to VideoWatch on {when} from IP {ip}.\n\n"
        f"If this wasn't you, reset your password at: {_APP_BASE_URL}/forgot-password\n"
    )
    threading.Thread(
        target=_send_email_simple,
        args=(email, "New sign-in to your VideoWatch account", html, text),
        daemon=True,
    ).start()


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

def _delete_thumb(url: str | None):
    if not url:
        return
    try:
        (THUMBS_DIR / hashlib.md5(url.encode()).hexdigest()).unlink(missing_ok=True)
    except Exception:
        pass

def _delete_thumbs_for_videos(db_conn, where_sql: str, params: tuple):
    rows = db_conn.execute(f"SELECT thumb FROM videos WHERE {where_sql}", params).fetchall()
    for r in rows:
        _delete_thumb(r["thumb"])


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
    max_pages:     int = 10
    scan_interval: int = 300   # seconds
    rule_include_keywords: str = ""
    rule_exclude_keywords: str = ""
    rule_min_duration: int = 0
    video_url_pattern: str = ""
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
    video_url_pattern: str | None = None
    scan_profile: str | None = None
    notify_enabled: bool | None = None

class MarkSeenIn(BaseModel):
    site_id: str | None = None

class AutomationToggleIn(BaseModel):
    enabled: bool

class LoginIn(BaseModel):
    username: str
    password: str
    remember_me: bool = False


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str


class ForgotPasswordIn(BaseModel):
    email: str


class ResetPasswordIn(BaseModel):
    token: str
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
    tos_accepted: bool = False
    referral_code: str | None = None


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
    raise HTTPException(status_code=501, detail="Use /api/downloads instead")
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
            "SELECT username, password_salt, password_hash, role, active, email_verified, onboarding_done, plan, ui_theme FROM users WHERE username=?",
            (username,),
        ).fetchone()
    return dict(row) if row else None


PLAN_LIMITS = {
    "free":      {"sites": 3,   "videos": 200,  "min_interval": 21600},
    "pro":       {"sites": 25,  "videos": 5000, "min_interval": 300},
    "unlimited": {"sites": None,"videos": None,  "min_interval": 300},
}

def _plan_limits(username: str) -> dict:
    row = _get_user(username)
    plan = (row.get("plan") or "free") if row else "free"
    return PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])


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
                # Super admins are always considered email-verified and onboarding done
                db.execute(
                    "UPDATE users SET email_verified=1, onboarding_done=1 WHERE role='super_admin'",
                )
                db.commit()
    except Exception as e:
        log.warning(f"Could not migrate default admin / site owners: {e}")


def _create_server_session(request: Request, username: str, role: str, ttl_days: int = 30) -> str:
    """Create a server-side session row and store the token in the cookie session."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    from datetime import timedelta as _td
    expires_at = (now + _td(days=ttl_days)).isoformat()
    with write_lock:
        with get_db() as db:
            db.execute(
                "INSERT INTO user_sessions (token, username, role, expires_at, created_at, last_seen) "
                "VALUES (?,?,?,?,?,?)",
                (token, username, role, expires_at, now.isoformat(), now.isoformat()),
            )
            db.commit()
    request.session["server_token"] = token
    return token


def _validate_server_session(request: Request) -> bool:
    """Return True if the server-side session token in the cookie is still valid."""
    token = request.session.get("server_token")
    if not token:
        return False
    now = datetime.now(timezone.utc)
    with get_db() as db:
        row = db.execute(
            "SELECT username, role, expires_at FROM user_sessions WHERE token=?", (token,)
        ).fetchone()
    if not row:
        return False
    try:
        if now > datetime.fromisoformat(row["expires_at"]):
            _delete_server_session(token)
            return False
    except Exception:
        return False
    # Refresh last_seen periodically (non-blocking best-effort)
    try:
        with write_lock:
            with get_db() as db:
                db.execute("UPDATE user_sessions SET last_seen=? WHERE token=?",
                           (now.isoformat(), token))
                db.commit()
    except Exception:
        pass
    # Re-sync cookie fields from DB in case they drifted
    request.session["auth_user"] = row["username"]
    request.session["auth_role"] = row["role"]
    return True


def _delete_server_session(token: str):
    with write_lock:
        with get_db() as db:
            db.execute("DELETE FROM user_sessions WHERE token=?", (token,))
            db.commit()


def is_authenticated(request: Request) -> bool:
    if not auth_enabled():
        return True
    # If the session has a server_token, validate it against the DB
    if request.session.get("server_token"):
        return _validate_server_session(request)
    # Legacy: cookie-only sessions (before server-side sessions were added)
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
    return _read_setting("auth_default_role") or "viewer"


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
    site_label = site.get("name") or site.get("url") or site.get("id")
    site_notify = site.get("notify_enabled")
    site_notify_off = site_notify is not None and int(site_notify) == 0

    # Webhook notification (admin-configured, global)
    enabled = (_read_setting("notify_enabled") or "0") == "1"
    webhook = (_read_setting("notify_webhook_url") or "").strip()
    if enabled and webhook and added > 0 and not site_notify_off:
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

    # Per-user email notification
    if added <= 0 or site_notify_off or not _email_configured():
        return
    owner = site.get("owner")
    if not owner:
        return
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT email, notify_new_videos FROM users WHERE username=? AND email_verified=1",
                (owner,),
            ).fetchone()
        if not row or not row["notify_new_videos"] or not row["email"]:
            return
        site_url = site.get("url", "")
        subject = f"VideoWatch: {added} new video{'s' if added != 1 else ''} on {site_label}"
        body_html = f"""
        <div style="font-family:sans-serif;max-width:520px;margin:0 auto">
          <h2 style="color:#0f766e">VideoWatch</h2>
          <p>A scan just finished for <strong>{site_label}</strong>.</p>
          <table style="border-collapse:collapse;width:100%">
            <tr><td style="padding:6px 0;color:#64748b">New videos added</td>
                <td style="padding:6px 0;font-weight:700">{added}</td></tr>
            <tr><td style="padding:6px 0;color:#64748b">Total found</td>
                <td style="padding:6px 0">{found}</td></tr>
            <tr><td style="padding:6px 0;color:#64748b">Site</td>
                <td style="padding:6px 0"><a href="{site_url}">{site_url}</a></td></tr>
          </table>
          <p style="margin-top:1.5rem">
            <a href="{_APP_BASE_URL}" style="background:#0f766e;color:#fff;padding:10px 22px;border-radius:8px;text-decoration:none;font-weight:700">
              View in VideoWatch
            </a>
          </p>
          <p style="color:#94a3b8;font-size:.8rem;margin-top:2rem">
            You're receiving this because you enabled email notifications in VideoWatch.<br>
            <a href="{_APP_BASE_URL}" style="color:#94a3b8">Manage notification settings</a>
          </p>
        </div>"""
        _send_email(row["email"], subject, body_html)
    except Exception as e:
        log.warning(f"Per-user scan email failed: {e}")

    # Web Push notification
    if added > 0 and not site_notify_off:
        owner = site.get("owner")
        if owner:
            threading.Thread(
                target=_push_new_videos,
                args=(owner, site_label, added, _APP_BASE_URL),
                daemon=True,
            ).start()

def _notify_scan_failure(site: dict, attempts: int):
    """Send one alert email to the site owner when repeated scans keep failing."""
    owner = site.get("owner")
    if not owner or not _email_configured():
        return
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT email, notify_new_videos FROM users WHERE username=? AND email_verified=1",
                (owner,),
            ).fetchone()
        if not row or not row["notify_new_videos"] or not row["email"]:
            return
        site_label = site.get("name") or site.get("url") or site.get("id")
        site_url = site.get("url", "")
        subject = f"VideoWatch: scan failing for {site_label}"
        body_html = f"""
        <div style="font-family:sans-serif;max-width:520px;margin:0 auto">
          <h2 style="color:#dc2626">VideoWatch — Scan Alert</h2>
          <p>The site <strong>{site_label}</strong> has failed to scan {attempts} time(s) in a row.</p>
          <table style="border-collapse:collapse;width:100%">
            <tr><td style="padding:6px 0;color:#64748b">Site</td>
                <td style="padding:6px 0"><a href="{site_url}">{site_url}</a></td></tr>
            <tr><td style="padding:6px 0;color:#64748b">Failed attempts</td>
                <td style="padding:6px 0;font-weight:700;color:#dc2626">{attempts}</td></tr>
          </table>
          <p>The site may be down, blocking scrapers, or requiring updated scan settings.</p>
          <p style="margin-top:1.5rem">
            <a href="{_APP_BASE_URL}" style="background:#0f766e;color:#fff;padding:10px 22px;border-radius:8px;text-decoration:none;font-weight:700">
              Open VideoWatch
            </a>
          </p>
          <p style="color:#94a3b8;font-size:.8rem;margin-top:2rem">
            You'll receive this alert once per failure streak. It won't repeat until the site recovers and fails again.
          </p>
        </div>"""
        _send_email(row["email"], subject, body_html)
        log.info(f"Scan failure alert sent to {row['email']} for {site_label}")
    except Exception as e:
        log.warning(f"Scan failure alert email failed: {e}")


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

def _audit(request: Request, action: str, detail: str = ""):
    try:
        username = current_user(request) or "anonymous"
        ip = _client_ip(request)
        with get_db() as db:
            db.execute(
                "INSERT INTO audit_log (timestamp, username, action, detail, ip) VALUES (?,?,?,?,?)",
                (now_iso(), username, action, detail, ip),
            )
            db.commit()
    except Exception as e:
        log.warning(f"Audit log write failed: {e}")


@router.get("/api/admin/audit")
def get_audit_log(request: Request, limit: int = 100, offset: int = 0):
    if not is_super_admin(request):
        raise HTTPException(403, "Super admin required")
    limit = max(1, min(limit, 500))
    with get_db() as db:
        total = db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        rows = db.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return {"total": total, "rows": [dict(r) for r in rows]}


@router.get("/api/admin/user-stats")
def admin_user_stats(request: Request):
    """Per-user usage stats (super_admin only)."""
    if not is_super_admin(request):
        raise HTTPException(403, "Super admin required")
    with get_db() as db:
        rows = db.execute("""
            SELECT
                u.username,
                u.role,
                u.email,
                u.email_verified,
                COALESCE(s.site_count, 0)  AS site_count,
                COALESCE(v.video_count, 0) AS video_count,
                COALESCE(l.scan_count, 0)  AS scan_count,
                l.last_scan
            FROM users u
            LEFT JOIN (
                SELECT owner, COUNT(*) AS site_count FROM sites GROUP BY owner
            ) s ON s.owner = u.username
            LEFT JOIN (
                SELECT sites.owner, COUNT(*) AS video_count
                FROM videos JOIN sites ON videos.site_id = sites.id
                GROUP BY sites.owner
            ) v ON v.owner = u.username
            LEFT JOIN (
                SELECT sites.owner, COUNT(*) AS scan_count, MAX(scan_log.scanned_at) AS last_scan
                FROM scan_log JOIN sites ON scan_log.site_id = sites.id
                GROUP BY sites.owner
            ) l ON l.owner = u.username
            ORDER BY v.video_count DESC NULLS LAST
        """).fetchall()
    return [dict(r) for r in rows]


@router.get("/api/admin/backup")
def download_backup(request: Request):
    """Stream a safe SQLite backup to the client (super_admin only)."""
    if not is_super_admin(request):
        raise HTTPException(403, "Super admin required")
    import io, sqlite3 as _sqlite3
    buf = io.BytesIO()
    src = _sqlite3.connect(str(DB_PATH))
    dst = _sqlite3.connect(":memory:")
    try:
        src.backup(dst)
        for line in dst.iterdump():
            buf.write((line + "\n").encode())
    finally:
        src.close()
        dst.close()
    buf.seek(0)
    from datetime import datetime as _dt
    stamp = _dt.now().strftime("%Y%m%d_%H%M%S")
    filename = f"videowatch_backup_{stamp}.sql"
    return Response(
        content=buf.read(),
        media_type="application/sql",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/api/admin/thumb-cleanup")
def thumb_cleanup(request: Request):
    """Delete cached thumbnail files with no matching video in the DB (super_admin only)."""
    if not is_super_admin(request):
        raise HTTPException(403, "Super admin required")
    with get_db() as db:
        known = {
            hashlib.md5(r["thumb"].encode()).hexdigest()
            for r in db.execute("SELECT thumb FROM videos WHERE thumb IS NOT NULL").fetchall()
        }
    removed = 0
    errors = 0
    for f in THUMBS_DIR.iterdir():
        if f.is_file() and f.name not in known:
            try:
                f.unlink()
                removed += 1
            except Exception:
                errors += 1
    _audit(request, "thumb_cleanup", f"removed={removed} errors={errors}")
    return {"ok": True, "removed": removed, "errors": errors}


@router.get("/api/admin/sites")
def admin_list_all_sites(request: Request):
    """Super-admin view: all sites across all users, grouped by owner."""
    if not is_super_admin(request):
        raise HTTPException(403, "Super admin required")
    with get_db() as db:
        rows = db.execute(
            "SELECT s.id, s.url, s.name, s.group_name, s.owner, s.added_at, "
            "COUNT(v.id) as total_videos "
            "FROM sites s LEFT JOIN videos v ON v.site_id=s.id "
            "GROUP BY s.id ORDER BY s.owner, s.added_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("/api/sites")
def list_sites(request: Request):
    with get_db() as db:
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
    try:
        return _add_site_impl(body, request)
    except HTTPException:
        raise
    except Exception as exc:
        log.exception(f"add_site error: {exc}")
        raise HTTPException(500, str(exc))

def _add_site_impl(body: SiteIn, request: Request):
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
    rule_min_duration = max(0, body.rule_min_duration or 0)
    profile = (body.scan_profile or "balanced").strip().lower()
    if profile not in {"fast", "balanced", "deep"}:
        profile = "balanced"
    with write_lock:
        with get_db() as db:
            owner = current_user(request) or (expected_auth_user().strip() or "admin")

            # Enforce plan site limit (super_admin is exempt)
            limits = _plan_limits(owner)
            site_count = db.execute("SELECT COUNT(*) FROM sites WHERE owner=?", (owner,)).fetchone()[0]
            if not is_super_admin(request):
                if limits["sites"] is not None:
                    if site_count >= limits["sites"]:
                        raise HTTPException(403, f"Free plan is limited to {limits['sites']} monitored sites. Upgrade to add more.")

            # Enforce plan minimum scan interval
            # First site gets 5-minute interval regardless of plan (onboarding UX)
            effective_min = 300 if site_count == 0 else limits["min_interval"]
            scan_interval = max(effective_min, max(60, body.scan_interval))

            if db.execute("SELECT id FROM sites WHERE url=? AND owner=?", (url, owner)).fetchone():
                raise HTTPException(409, "Site already monitored")
            site_id = short_id(f"{owner}:{url}")
            notify_enabled = 1 if body.notify_enabled else 0
            db.execute(
                "INSERT INTO sites (id, url, name, group_name, added_at, max_pages, scan_interval, "
                "rule_include_keywords, rule_exclude_keywords, rule_min_duration, scan_profile, notify_enabled, owner, video_url_pattern) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    site_id,
                    url,
                    (body.name or "").strip(),
                    (body.group_name or "").strip(),
                    now_iso(),
                    max_pages,
                    scan_interval,
                    (body.rule_include_keywords or "").strip(),
                    (body.rule_exclude_keywords or "").strip(),
                    rule_min_duration,
                    profile,
                    notify_enabled,
                    owner,
                    (body.video_url_pattern or "").strip(),
                ))
            # Auto-enable auto-scan when the very first site is added
            site_count = db.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
            if site_count == 1:
                db.execute(
                    "INSERT INTO app_settings (key, value) VALUES ('autoscan_enabled', '1') "
                    "ON CONFLICT(key) DO UPDATE SET value='1'",
                )
            db.commit()
    _audit(request, "site_add", f"url={url} name={body.name or ''}")
    return {"id": site_id, "url": url, "name": body.name or "",
            "group_name": body.group_name or "", "max_pages": max_pages, "scan_interval": scan_interval,
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
        request.session["auth_role"] = _sanitize_role(row.get("role") if row else None) or "viewer"
        return {
            "ok": True,
            "authenticated": True,
            "user": request.session["auth_user"],
            "role": request.session["auth_role"],
        }

    if not validate_credentials(body.username, body.password):
        _audit(request, "login_failed", f"username={body.username}")
        raise HTTPException(401, "Invalid username or password")

    row = _get_user(body.username)
    # Block login if email verification is configured and not yet verified
    if _email_configured() and row and not row.get("email_verified"):
        # super_admin bootstrapped accounts are always exempt
        if row.get("role") != "super_admin":
            raise HTTPException(403, "Please verify your email before logging in. Check your inbox.")

    from datetime import timedelta
    ttl_days = 30 if body.remember_me else 1
    role = _sanitize_role(row.get("role") if row else None) or "viewer"
    request.session["auth_user"] = body.username
    request.session["auth_role"] = role
    request.session["session_expires_at"] = (datetime.now(timezone.utc) + timedelta(days=ttl_days)).isoformat()
    _create_server_session(request, body.username, role, ttl_days=ttl_days)
    _audit(request, "login", f"role={request.session['auth_role']} remember_me={body.remember_me}")
    # Email login notification if user has a verified email
    if row and row.get("email") and row.get("email_verified"):
        _notify_new_login(body.username, row["email"], _client_ip(request))
    return {
        "ok": True,
        "authenticated": True,
        "user": body.username,
        "role": request.session["auth_role"],
    }


@router.post("/api/auth/logout")
def auth_logout(request: Request):
    _audit(request, "logout", "")
    token = request.session.get("server_token")
    if token:
        _delete_server_session(token)
    request.session.clear()
    return {"ok": True, "authenticated": False}


@router.get("/api/auth/sessions")
def list_sessions(request: Request):
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    username = request.session.get("auth_user") or ""
    if request.session.get("server_token"):
        with get_db() as db:
            row = db.execute(
                "SELECT username FROM user_sessions WHERE token=?",
                (request.session["server_token"],)
            ).fetchone()
            if row:
                username = row["username"]
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    current_token = request.session.get("server_token", "")
    with get_db() as db:
        rows = db.execute(
            "SELECT token, created_at, last_seen, expires_at FROM user_sessions WHERE username=? ORDER BY last_seen DESC",
            (username,)
        ).fetchall()
    sessions = []
    for r in rows:
        sessions.append({
            "token": r["token"],
            "label": "Browser session",
            "device_type": "desktop",
            "created_at": r["created_at"],
            "last_seen": r["last_seen"],
            "expires_at": r["expires_at"],
            "is_current": r["token"] == current_token,
        })
    return {"sessions": sessions}


@router.delete("/api/auth/sessions/{token}")
def revoke_session(token: str, request: Request):
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    username = request.session.get("auth_user") or ""
    if request.session.get("server_token"):
        with get_db() as db:
            row = db.execute(
                "SELECT username FROM user_sessions WHERE token=?",
                (request.session["server_token"],)
            ).fetchone()
            if row:
                username = row["username"]
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    with write_lock:
        with get_db() as db:
            row = db.execute(
                "SELECT username FROM user_sessions WHERE token=?", (token,)
            ).fetchone()
            if not row or row["username"] != username:
                raise HTTPException(status_code=404, detail="Session not found")
            db.execute("DELETE FROM user_sessions WHERE token=?", (token,))
            db.commit()
    _audit(request, "revoke_session", token[:8] + "…")
    return {"ok": True}


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
    if not body.tos_accepted:
        raise HTTPException(400, "You must accept the Terms of Service to create an account")
    with get_db() as db:
        if db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise HTTPException(409, "Username already taken")
        if db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            raise HTTPException(409, "An account with that email already exists")
    _create_user_record(username, body.password, "viewer")
    # Generate referral code and track referrer
    ref_code = secrets.token_urlsafe(8)
    referred_by = None
    ref_param = (body.__dict__.get("referral_code") or "").strip()
    if ref_param:
        with get_db() as db:
            ref_row = db.execute("SELECT username FROM users WHERE referral_code=?", (ref_param,)).fetchone()
            if ref_row:
                referred_by = ref_row["username"]
    # Store email and set verified status
    from datetime import timedelta
    expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    token = secrets.token_urlsafe(32)
    with write_lock:
        with get_db() as db:
            db.execute(
                "UPDATE users SET email=?, email_verified=0, tos_accepted_at=?, referral_code=?, referred_by=? WHERE username=?",
                (email, now_iso(), ref_code, referred_by, username)
            )
            db.execute(
                "INSERT OR REPLACE INTO email_verifications (token, username, expires_at) VALUES (?,?,?)",
                (token, username, expires),
            )
            db.commit()
    log.info(f"New user registered: {username} <{email}>")
    _audit(request, "register", f"username={username} email={email}")
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


@router.post("/api/auth/forgot-password")
def forgot_password(body: ForgotPasswordIn, request: Request):
    _check_rate_limit(_client_ip(request), max_attempts=3, window_seconds=600)
    email = (body.email or "").strip().lower()
    if not email:
        raise HTTPException(400, "Email address is required")
    # Look up user by email — always return 200 to avoid user enumeration
    with get_db() as db:
        row = db.execute("SELECT username FROM users WHERE email=?", (email,)).fetchone()
    if row and _email_configured():
        username = row["username"]
        from datetime import timedelta
        token = secrets.token_urlsafe(32)
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        with write_lock:
            with get_db() as db:
                db.execute(
                    "INSERT OR REPLACE INTO password_resets (token, username, expires_at) VALUES (?,?,?)",
                    (token, username, expires),
                )
                db.commit()
        try:
            _send_reset_email(email, username, token)
        except Exception as exc:
            log.error(f"Failed to send password reset email to {email}: {exc}")
    return {"ok": True, "message": "If that email is registered, a reset link has been sent."}


@router.post("/api/auth/reset-password")
def reset_password(body: ResetPasswordIn, request: Request):
    _check_rate_limit(_client_ip(request), max_attempts=5, window_seconds=300)
    if not body.new_password or len(body.new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    if body.new_password != body.confirm_password:
        raise HTTPException(400, "Passwords do not match")
    now = datetime.now(timezone.utc).isoformat()
    with write_lock:
        with get_db() as db:
            row = db.execute(
                "SELECT username, expires_at FROM password_resets WHERE token=?", (body.token,)
            ).fetchone()
            if not row:
                raise HTTPException(400, "Invalid or already used reset link")
            if row["expires_at"] < now:
                db.execute("DELETE FROM password_resets WHERE token=?", (body.token,))
                db.commit()
                raise HTTPException(400, "Reset link has expired. Please request a new one.")
            username = row["username"]
            salt = secrets.token_bytes(16)
            salt_b64 = base64.b64encode(salt).decode("ascii")
            hash_b64 = _pbkdf2_hash(body.new_password, salt_b64)
            db.execute(
                "UPDATE users SET password_salt=?, password_hash=?, updated_at=? WHERE username=?",
                (salt_b64, hash_b64, now_iso(), username),
            )
            db.execute("DELETE FROM password_resets WHERE token=?", (body.token,))
            db.commit()
    log.info(f"Password reset for user: {username}")
    # No request object here — log with anonymous ip
    with get_db() as db:
        db.execute(
            "INSERT INTO audit_log (timestamp, username, action, detail, ip) VALUES (?,?,?,?,?)",
            (now_iso(), username, "password_reset", "via email token", None),
        )
        db.commit()
    return {"ok": True}


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
    # Notify via email
    if user_row and user_row.get("email") and user_row.get("email_verified"):
        _notify_password_changed(username, user_row["email"], _client_ip(request))
    return {"ok": True}


@router.get("/api/auth/status")
def auth_status(request: Request):
    enabled = auth_enabled()
    authenticated = is_authenticated(request)
    onboarding_done = True
    ui_theme = "light"
    if authenticated:
        username = request.session.get("auth_user")
        if username:
            row = _get_user(username)
            onboarding_done = bool(row.get("onboarding_done")) if row else True
            ui_theme = (row.get("ui_theme") or "light") if row else "light"
    return {
        "enabled": enabled,
        "authenticated": authenticated,
        "user": request.session.get("auth_user") if authenticated else None,
        "role": current_role(request) if authenticated else None,
        "onboarding_done": onboarding_done,
        "ui_theme": ui_theme,
    }



# ── Google OAuth ──────────────────────────────────────────────────────────────
_GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
_GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
_GOOGLE_REDIRECT_URI  = os.environ.get(
    "GOOGLE_REDIRECT_URI",
    "https://videowatch.duckdns.org/auth/google/callback"
)
_GOOGLE_AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO  = "https://www.googleapis.com/oauth2/v3/userinfo"


@router.get("/auth/google")
def google_login(request: Request, mobile: str = ""):
    if not _GOOGLE_CLIENT_ID:
        raise HTTPException(503, "Google OAuth not configured")
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    request.session["oauth_mobile"] = bool(mobile)
    params = {
        "client_id":     _GOOGLE_CLIENT_ID,
        "redirect_uri":  _GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope":         "openid email profile",
        "state":         state,
        "access_type":   "online",
        "prompt":        "select_account",
    }
    from urllib.parse import urlencode as _urlencode
    url = _GOOGLE_AUTH_URL + "?" + _urlencode(params)
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url)


@router.get("/auth/google/callback")
async def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    from fastapi.responses import RedirectResponse
    is_mobile = request.session.get("oauth_mobile", False)
    def _err(reason: str):
        if is_mobile:
            return RedirectResponse(f"videowatch://auth/callback?error={reason}")
        return RedirectResponse(f"/static/login.html?error={reason}")
    if error or not code:
        return _err("google_denied")
    if state != request.session.get("oauth_state"):
        return _err("invalid_state")
    request.session.pop("oauth_state", None)

    # Exchange code for token
    async with httpx.AsyncClient() as client:
        token_res = await client.post(_GOOGLE_TOKEN_URL, data={
            "code":          code,
            "client_id":     _GOOGLE_CLIENT_ID,
            "client_secret": _GOOGLE_CLIENT_SECRET,
            "redirect_uri":  _GOOGLE_REDIRECT_URI,
            "grant_type":    "authorization_code",
        })
        if token_res.status_code != 200:
            return _err("token_exchange")
        token_data = token_res.json()

        user_res = await client.get(_GOOGLE_USERINFO, headers={
            "Authorization": f"Bearer {token_data['access_token']}"
        })
        if user_res.status_code != 200:
            return _err("userinfo")
        guser = user_res.json()

    email    = guser.get("email", "").lower().strip()
    name     = guser.get("name") or guser.get("given_name") or email.split("@")[0]
    google_id = guser.get("sub", "")

    if not email:
        return _err("no_email")

    # Find or create user
    with write_lock:
        with get_db() as db:
            row = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if row:
                username = row["username"]
                role     = row["role"]
            else:
                # Create new account from Google profile
                username = re.sub(r"[^a-z0-9_]", "", name.lower().replace(" ", "_"))[:20] or "user"
                # Ensure unique username
                base = username
                i = 1
                while db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                    username = f"{base}{i}"; i += 1
                salt = secrets.token_hex(16)
                # Random password — user can't log in with password, only Google
                pw_hash = hashlib.sha256((salt + secrets.token_hex(32)).encode()).hexdigest()
                now = datetime.now(timezone.utc).isoformat()
                db.execute("""
                    INSERT INTO users (username, password_salt, password_hash, role, active,
                                       created_at, updated_at, email, email_verified)
                    VALUES (?, ?, ?, 'viewer', 1, ?, ?, ?, 1)
                """, (username, salt, pw_hash, now, now, email))
                role = "viewer"
            db.commit()

    from datetime import timedelta
    request.session["auth_user"] = username
    request.session["auth_role"] = role
    request.session["session_expires_at"] = (
        datetime.now(timezone.utc) + timedelta(days=30)
    ).isoformat()
    _create_server_session(request, username, role, ttl_days=30)
    _audit(request, "google_login", f"email={email}")
    is_mobile = request.session.pop("oauth_mobile", False)
    if is_mobile:
        from fastapi.responses import RedirectResponse as _RR
        return _RR("videowatch://auth/callback?success=1")
    return HTMLResponse("""<!DOCTYPE html><html><head>
<meta http-equiv="refresh" content="0;url=/" />
<script>window.location.replace('/');</script>
</head><body>Signing you in…</body></html>""")


@router.get("/api/auth/google/config")
def google_oauth_config():
    """Returns whether Google OAuth is configured so the frontend can show/hide the button."""
    return {"enabled": bool(_GOOGLE_CLIENT_ID)}


@router.post("/api/auth/complete-onboarding")
def complete_onboarding(request: Request):
    if not is_authenticated(request):
        raise HTTPException(401, "Authentication required")
    username = request.session.get("auth_user")
    with write_lock:
        with get_db() as db:
            db.execute("UPDATE users SET onboarding_done=1 WHERE username=?", (username,))
            db.commit()
    return {"ok": True}


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
    _audit(request, "user_create", f"username={username} role={role}")
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
    _audit(request, "user_update", f"username={username} role={role} active={active}")
    return {"ok": True, "username": username, "role": role, "active": bool(active)}

@router.patch("/api/admin/users/{username}/plan")
def admin_set_user_plan(username: str, body: dict, request: Request):
    if not is_super_admin(request):
        raise HTTPException(403, "Super admin only")
    plan = (body.get("plan") or "free").strip().lower()
    if plan not in PLAN_LIMITS:
        raise HTTPException(400, f"Invalid plan. Must be one of: {', '.join(PLAN_LIMITS)}")
    with write_lock:
        with get_db() as db:
            if not db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                raise HTTPException(404, "User not found")
            db.execute("UPDATE users SET plan=?, updated_at=? WHERE username=?", (plan, now_iso(), username))
            db.commit()
    _audit(request, "admin_plan_change", f"username={username} plan={plan}")
    return {"ok": True, "username": username, "plan": plan}


@router.get("/api/admin/billing/stats")
def admin_billing_stats(request: Request):
    if not is_super_admin(request):
        raise HTTPException(403, "Super admin only")
    with get_db() as db:
        plan_rows = db.execute(
            "SELECT COALESCE(plan,'free') as plan, COUNT(*) as count FROM users WHERE active=1 GROUP BY plan"
        ).fetchall()
        total_users = db.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]
        total_sites = db.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
        total_videos = db.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        recent_users = db.execute(
            """SELECT username, email, COALESCE(plan,'free') as plan, created_at, active,
                      (SELECT COUNT(*) FROM sites WHERE owner=users.username) as site_count,
                      (SELECT COUNT(*) FROM videos v JOIN sites s ON v.site_id=s.id WHERE s.owner=users.username) as video_count
               FROM users ORDER BY created_at DESC LIMIT 100"""
        ).fetchall()
    plans = {r["plan"]: r["count"] for r in plan_rows}
    return {
        "plans": plans,
        "total_users": total_users,
        "total_sites": total_sites,
        "total_videos": total_videos,
        "users": [dict(r) for r in recent_users],
        "stripe_connected": False,  # flip to True once Stripe keys are added
    }


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
            if body.scan_interval is not None:
                owner = row["owner"] or current_user(request)
                min_interval = _plan_limits(owner)["min_interval"]
                updates["scan_interval"] = max(min_interval, max(60, body.scan_interval))
            if body.rule_include_keywords is not None:
                updates["rule_include_keywords"] = body.rule_include_keywords.strip()
            if body.rule_exclude_keywords is not None:
                updates["rule_exclude_keywords"] = body.rule_exclude_keywords.strip()
            if body.rule_min_duration is not None:
                updates["rule_min_duration"] = max(0, body.rule_min_duration)
            if body.video_url_pattern is not None:
                updates["video_url_pattern"] = body.video_url_pattern.strip()
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
            _delete_thumbs_for_videos(db, "site_id=?", (site_id,))
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
    _audit(request, "site_delete", f"site_id={site_id}")
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
                watched_only: bool = False,
                archived_only: bool = False,
                ignored_only: bool = False,
                tag: str = "",
                sort: str = ""):
    offset = (page - 1) * per_page
    with get_db() as db:
        filters = []
        params = []
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
            fts_available = False
            try:
                db.execute("SELECT 1 FROM videos_fts LIMIT 1")
                fts_available = True
            except Exception:
                pass
            if fts_available:
                fts_query = " OR ".join(
                    f'"{word}"*' if word.isalnum() else f'"{word}"'
                    for word in search.split()
                    if word
                ) or f'"{search}"'
                filters.append(
                    "(videos.rowid IN (SELECT rowid FROM videos_fts WHERE videos_fts MATCH ?) "
                    "OR videos.url LIKE ? OR videos.platform LIKE ? "
                    "OR sites.name LIKE ? OR sites.group_name LIKE ?)"
                )
                params.extend([fts_query, search_term, search_term, search_term, search_term])
            else:
                filters.append(
                    "(videos.title LIKE ? OR videos.cast_names LIKE ? OR videos.url LIKE ? "
                    "OR videos.platform LIKE ? OR sites.name LIKE ? OR sites.group_name LIKE ?)"
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
        if watched_only:
            filters.append("COALESCE(videos.is_watched, 0)=1")
        if archived_only:
            filters.append("COALESCE(videos.is_archived, 0)=1")
        if ignored_only:
            filters.append("COALESCE(videos.is_ignored, 0)=1")
        if tag:
            filters.append(
                "EXISTS (SELECT 1 FROM video_tags vt WHERE vt.video_id=videos.id AND vt.tag=? AND vt.owner=?)"
            )
            params.extend([tag.strip().lower(), current_user(request) or ""])

        where = ("WHERE " + " AND ".join(filters)) if filters else ""
        if sort == "last_watched":
            order = "ORDER BY COALESCE(videos.last_watched_at, '') DESC"
        elif sort == "found":
            order = "ORDER BY videos.found_at DESC"
        elif sort == "oldest":
            order = "ORDER BY SUBSTR(COALESCE(videos.released_at, videos.found_at), 1, 19) ASC"
        elif sort == "duration_desc":
            order = "ORDER BY COALESCE(videos.duration, 0) DESC"
        elif sort == "duration_asc":
            order = "ORDER BY COALESCE(videos.duration, 0) ASC"
        elif sort == "title_asc":
            order = "ORDER BY LOWER(videos.title) ASC"
        else:
            order = "ORDER BY SUBSTR(COALESCE(videos.released_at, videos.found_at), 1, 19) DESC"

        total = db.execute(
            f"SELECT COUNT(*) FROM videos LEFT JOIN sites ON videos.site_id = sites.id {where}",
            params).fetchone()[0]
        rows = db.execute(
            f"SELECT videos.* FROM videos LEFT JOIN sites ON videos.site_id = sites.id {where} {order} LIMIT ? OFFSET ?",
            params + [per_page, offset]).fetchall()
        owner = current_user(request) or ""
        video_ids = [r["id"] for r in rows]
        tags_map: dict[str, list[str]] = {}
        if video_ids:
            placeholders = ",".join("?" * len(video_ids))
            tag_rows = db.execute(
                f"SELECT video_id, tag FROM video_tags WHERE video_id IN ({placeholders}) AND owner=? ORDER BY tag",
                video_ids + [owner],
            ).fetchall()
            for tr in tag_rows:
                tags_map.setdefault(tr["video_id"], []).append(tr["tag"])

    videos_out = [dict(r) for r in rows]
    for v in videos_out:
        v["tags"] = tags_map.get(v["id"], [])

    return {
        "videos":      videos_out,
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

@router.delete("/api/videos/{video_id}")
def delete_video(video_id: str, request: Request):
    username = _api_auth(request)
    with write_lock:
        with get_db() as db:
            row = db.execute(
                "SELECT v.id FROM videos v JOIN sites s ON s.id=v.site_id WHERE v.id=? AND s.owner=?",
                (video_id, username)
            ).fetchone()
            if not row:
                raise HTTPException(404, "Video not found")
            db.execute("DELETE FROM videos WHERE id=?", (video_id,))
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
                _delete_thumbs_for_videos(db, "site_id=?", (site_id,))
                db.execute("DELETE FROM videos WHERE site_id=?", (site_id,))
                db.execute("UPDATE sites SET last_scan=NULL WHERE id=?", (site_id,))
            else:
                _delete_thumbs_for_videos(db, "1=1", ())
                db.execute("DELETE FROM videos")
                db.execute("UPDATE sites SET last_scan=NULL")
            db.commit()
    return {"ok": True}

# ── Background Sync Task Managers ─────────────────────────────────────────────

# ── Scan queue (single worker thread) ─────────────────────────────────────────
_scan_queue: queue.Queue = queue.Queue()
_scan_queue_lock = threading.Lock()
_scan_queue_items: list[dict] = []   # shadow list for status queries
_scan_running: dict | None = None    # currently running job


_RETRY_DELAYS = [30, 120, 600]   # seconds: 30s, 2min, 10min


def _scan_worker():
    global _scan_running
    while True:
        job = _scan_queue.get()
        if job is None:
            break
        with _scan_queue_lock:
            _scan_running = job
            if job in _scan_queue_items:
                _scan_queue_items.remove(job)
        site = job["site"]
        attempt = job.get("attempt", 0)
        try:
            _run_scan_one_sync(site)
            # Reset failure streak on success
            site_id = site.get("id")
            if site_id:
                with write_lock:
                    with get_db() as db:
                        db.execute(
                            "UPDATE sites SET consecutive_failures=0, alert_sent=0 WHERE id=?",
                            (site_id,),
                        )
                        db.commit()
        except Exception as e:
            log.error(f"Scan failed for {site.get('url')} (attempt {attempt + 1}): {e}")
            if attempt < len(_RETRY_DELAYS):
                delay = _RETRY_DELAYS[attempt]
                log.info(f"Retrying {site.get('url')} in {delay}s (attempt {attempt + 2}/{len(_RETRY_DELAYS) + 1})")
                retry_job = {"site": site, "attempt": attempt + 1, "retry_after": time.monotonic() + delay}
                def _schedule_retry(j=retry_job, d=delay):
                    time.sleep(d)
                    with _scan_queue_lock:
                        _scan_queue_items.append(j)
                    _scan_queue.put(j)
                threading.Thread(target=_schedule_retry, daemon=True, name=f"scan-retry-{attempt+1}").start()
            else:
                log.error(f"Giving up on {site.get('url')} after {attempt + 1} attempts")
                # Increment consecutive failure streak and alert once
                site_id = site.get("id")
                if site_id:
                    with write_lock:
                        with get_db() as db:
                            db.execute(
                                "UPDATE sites SET consecutive_failures=consecutive_failures+1 WHERE id=?",
                                (site_id,),
                            )
                            db.commit()
                            row = db.execute(
                                "SELECT consecutive_failures, alert_sent FROM sites WHERE id=?",
                                (site_id,),
                            ).fetchone()
                    if row and row["consecutive_failures"] >= 3 and not row["alert_sent"]:
                        _notify_scan_failure(site, row["consecutive_failures"])
                        with write_lock:
                            with get_db() as db:
                                db.execute("UPDATE sites SET alert_sent=1 WHERE id=?", (site_id,))
                                db.commit()
        finally:
            with _scan_queue_lock:
                _scan_running = None
            _scan_queue.task_done()


_scan_worker_thread = threading.Thread(target=_scan_worker, daemon=True, name="scan-worker")
_scan_worker_thread.start()


def _enqueue_scan(site: dict, fresh: bool = False) -> bool:
    """Add a site to the scan queue. Returns False if already queued/running."""
    site_id = site.get("id")
    with _scan_queue_lock:
        if _scan_running and _scan_running.get("site", {}).get("id") == site_id:
            return False
        if any(j.get("site", {}).get("id") == site_id for j in _scan_queue_items):
            return False
        job = {"site": site, "fresh": fresh}
        _scan_queue_items.append(job)
    _scan_queue.put(job)
    return True


def _enqueue_scan_all(owner: str | None = None):
    """Enqueue all sites for an owner (or all sites if owner is None)."""
    with get_db() as db:
        if owner:
            sites = [dict(r) for r in db.execute("SELECT * FROM sites WHERE owner=?", (owner,))]
        else:
            sites = [dict(r) for r in db.execute("SELECT * FROM sites")]
    for site in sites:
        _enqueue_scan(site)


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
async def scan_all(request: Request):
    _check_rate_limit(_client_ip(request), max_attempts=10, window_seconds=60)
    user = current_user(request) or ""
    owner = None if is_super_admin(request) else user
    _enqueue_scan_all(owner)
    _audit(request, "scan_all", f"owner={owner or 'all'}")
    return {"ok": True, "message": "Scan queued"}

@router.post("/api/scan/fresh")
async def fresh_scan_all(request: Request):
    user = current_user(request) or ""
    with write_lock:
        with get_db() as db:
            if is_super_admin(request):
                deleted = db.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
                db.execute("DELETE FROM videos")
                db.execute("UPDATE sites SET last_scan=NULL")
            else:
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
    owner = None if is_super_admin(request) else user
    _enqueue_scan_all(owner)
    return {"ok": True, "message": "Fresh scan queued", "deleted": deleted}

@router.post("/api/scan/{site_id}")
async def scan_one(site_id: str, request: Request):
    _check_rate_limit(_client_ip(request), max_attempts=10, window_seconds=60)
    with get_db() as db:
        site = db.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()
    if not site:
        raise HTTPException(404, "Site not found")
    if not is_super_admin(request) and site["owner"] != current_user(request):
        raise HTTPException(403, "Not authorised to scan this site")
    queued = _enqueue_scan(dict(site))
    msg = f"Queued {site['url']}" if queued else f"{site['url']} is already queued or running"
    if queued:
        _audit(request, "scan_site", f"site_id={site_id} url={site['url']}")
    return {"ok": True, "queued": queued, "message": msg}

@router.get("/api/scan/queue")
def get_scan_queue(request: Request):
    """Return current queue state."""
    if not is_authenticated(request):
        raise HTTPException(401)
    with _scan_queue_lock:
        running = None
        if _scan_running:
            s = _scan_running.get("site", {})
            running = {"site_id": s.get("id"), "name": s.get("name") or s.get("url"), "url": s.get("url")}
        pending = [
            {
                "site_id": j["site"].get("id"),
                "name": j["site"].get("name") or j["site"].get("url"),
                "url": j["site"].get("url"),
                "attempt": j.get("attempt", 0),
                "is_retry": j.get("attempt", 0) > 0,
            }
            for j in _scan_queue_items
        ]
    return {"running": running, "pending": pending, "total": len(pending) + (1 if running else 0)}

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
def scan_status(request: Request):
    with get_db() as db:
        if is_super_admin(request):
            rows = db.execute(
                "SELECT scan_log.*, sites.name as site_name, sites.url as site_url "
                "FROM scan_log LEFT JOIN sites ON scan_log.site_id = sites.id "
                "ORDER BY scanned_at DESC LIMIT 50"
            ).fetchall()
        else:
            owner = current_user(request)
            rows = db.execute(
                "SELECT scan_log.*, sites.name as site_name, sites.url as site_url "
                "FROM scan_log LEFT JOIN sites ON scan_log.site_id = sites.id "
                "WHERE sites.owner=? ORDER BY scanned_at DESC LIMIT 50",
                (owner,)
            ).fetchall()
    return [dict(r) for r in rows]


@router.get("/api/scan/health")
def scan_health(request: Request, limit: int = 20):
    limit = max(5, min(limit, 100))
    with get_db() as db:
        if is_super_admin(request):
            params_overall = (limit,)
        else:
            owner = current_user(request)
            params_overall = (owner, limit)

        overall = db.execute(
            "SELECT COUNT(*) as runs, "
            "AVG(CASE WHEN message LIKE 'ERROR:%' THEN 0.0 ELSE 1.0 END) as success_rate, "
            "AVG(found) as avg_found, AVG(added) as avg_added "
            "FROM (SELECT scan_log.id, scan_log.message, scan_log.found, scan_log.added "
            "FROM scan_log JOIN sites ON scan_log.site_id=sites.id "
            + ("" if is_super_admin(request) else "WHERE sites.owner=? ") +
            "ORDER BY scan_log.id DESC LIMIT ?)",
            params_overall,
        ).fetchone()

        if is_super_admin(request):
            health_where = ""
            health_params = ()
        else:
            health_where = "WHERE s.owner=?"
            health_params = (current_user(request),)

        per_site = [dict(r) for r in db.execute(
            "SELECT s.id as site_id, COALESCE(NULLIF(s.name,''), s.url) as site_name, "
            "COUNT(l.id) as runs, "
            "ROUND(AVG(CASE WHEN l.message LIKE 'ERROR:%' THEN 0.0 ELSE 1.0 END), 3) as success_rate, "
            "ROUND(AVG(l.found), 2) as avg_found, "
            "ROUND(AVG(l.added), 2) as avg_added, "
            "MAX(l.scanned_at) as last_scan "
            f"FROM sites s {health_where} "
            "LEFT JOIN scan_log l ON l.site_id=s.id "
            "GROUP BY s.id ORDER BY runs DESC, last_scan DESC",
            health_params,
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


@router.get("/api/user/notifications")
def get_user_notifications(request: Request):
    if not is_authenticated(request):
        raise HTTPException(401, "Authentication required")
    username = current_user(request)
    with get_db() as db:
        row = db.execute(
            "SELECT email, email_verified, notify_new_videos, plan FROM users WHERE username=?",
            (username,),
        ).fetchone()
    if not row:
        raise HTTPException(404)
    return {
        "notify_new_videos": bool(row["notify_new_videos"]),
        "email": row["email"] or "",
        "email_verified": bool(row["email_verified"]),
        "plan": row["plan"] or "free",
    }


@router.post("/api/user/notifications")
def set_user_notifications(request: Request, body: dict):
    if not is_authenticated(request):
        raise HTTPException(401, "Authentication required")
    username = current_user(request)
    notify = bool(body.get("notify_new_videos", False))
    with get_db() as db:
        row = db.execute(
            "SELECT email_verified FROM users WHERE username=?", (username,)
        ).fetchone()
        if notify and (not row or not row["email_verified"]):
            raise HTTPException(400, "A verified email address is required to enable email notifications.")
        db.execute(
            "UPDATE users SET notify_new_videos=? WHERE username=?",
            (1 if notify else 0, username),
        )
    return {"ok": True, "notify_new_videos": notify}


@router.patch("/api/user/prefs")
def set_user_prefs(request: Request, body: dict):
    if not is_authenticated(request):
        raise HTTPException(401)
    username = current_user(request)
    theme = body.get("ui_theme")
    if theme not in ("light", "dark"):
        raise HTTPException(400, "Invalid theme")
    with write_lock:
        with get_db() as db:
            db.execute("UPDATE users SET ui_theme=? WHERE username=?", (theme, username))
            db.commit()
    return {"ok": True, "ui_theme": theme}


@router.patch("/api/user/email")
def update_user_email(request: Request, body: dict):
    if not is_authenticated(request):
        raise HTTPException(401)
    email = (body.get("email") or "").strip().lower()
    if not email or not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        raise HTTPException(400, "A valid email address is required")
    username = current_user(request)
    with write_lock:
        with get_db() as db:
            existing = db.execute("SELECT username FROM users WHERE email=? AND username!=?", (email, username)).fetchone()
            if existing:
                raise HTTPException(409, "That email is already in use by another account")
            db.execute(
                "UPDATE users SET email=?, email_verified=0, updated_at=? WHERE username=?",
                (email, now_iso(), username),
            )
            db.commit()
    # Send verification email if configured
    if _email_configured():
        token = secrets.token_urlsafe(32)
        expires = (datetime.now(timezone.utc).replace(microsecond=0).isoformat())
        with write_lock:
            with get_db() as db:
                db.execute("DELETE FROM email_verifications WHERE username=?", (username,))
                from datetime import timedelta
                exp = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
                db.execute("INSERT INTO email_verifications (token, username, expires_at) VALUES (?,?,?)", (token, username, exp))
                db.commit()
        threading.Thread(target=_send_verification_email, args=(email, username, token), daemon=True).start()
        return {"ok": True, "email": email, "verification_sent": True}
    return {"ok": True, "email": email, "verification_sent": False}


@router.post("/api/user/delete-account")
def delete_own_account(request: Request, body: dict):
    if not is_authenticated(request):
        raise HTTPException(401)
    username = current_user(request)
    if is_super_admin(request):
        raise HTTPException(403, "Super admin account cannot be self-deleted. Use the Users panel.")
    password = (body or {}).get("password", "")
    if not validate_credentials(username, password):
        raise HTTPException(403, "Incorrect password")
    with write_lock:
        with get_db() as db:
            # Delete all user data
            site_ids = [r["id"] for r in db.execute("SELECT id FROM sites WHERE owner=?", (username,)).fetchall()]
            for sid in site_ids:
                _delete_thumbs_for_videos(db, "site_id=?", (sid,))
            if site_ids:
                placeholders = ",".join("?" * len(site_ids))
                db.execute(f"DELETE FROM videos WHERE site_id IN ({placeholders})", site_ids)
                db.execute(f"DELETE FROM scan_log WHERE site_id IN ({placeholders})", site_ids)
                db.execute(f"DELETE FROM sites WHERE id IN ({placeholders})", site_ids)
            db.execute("DELETE FROM video_tags WHERE owner=?", (username,))
            db.execute("DELETE FROM email_verifications WHERE username=?", (username,))
            db.execute("DELETE FROM password_resets WHERE username=?", (username,))
            db.execute("DELETE FROM users WHERE username=?", (username,))
            db.commit()
    request.session.clear()
    _audit(request, "account_deleted", f"username={username}")
    return {"ok": True}


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


@router.get("/api/tags")
def list_tags(request: Request):
    """Return all unique tags for the current user."""
    if not is_authenticated(request):
        raise HTTPException(401)
    owner = current_user(request)
    with get_db() as db:
        rows = db.execute(
            "SELECT tag, COUNT(*) as count FROM video_tags WHERE owner=? GROUP BY tag ORDER BY tag",
            (owner,),
        ).fetchall()
    return [{"tag": r["tag"], "count": r["count"]} for r in rows]


@router.get("/api/videos/{video_id}/tags")
def get_video_tags(video_id: str, request: Request):
    if not is_authenticated(request):
        raise HTTPException(401)
    owner = current_user(request)
    with get_db() as db:
        rows = db.execute(
            "SELECT tag FROM video_tags WHERE video_id=? AND owner=? ORDER BY tag",
            (video_id, owner),
        ).fetchall()
    return [r["tag"] for r in rows]


@router.post("/api/videos/{video_id}/tags")
def add_video_tag(video_id: str, request: Request, body: dict):
    if not is_authenticated(request):
        raise HTTPException(401)
    tag = (body.get("tag") or "").strip().lower()[:40]
    if not tag:
        raise HTTPException(400, "Tag cannot be empty")
    owner = current_user(request)
    with get_db() as db:
        if not db.execute("SELECT 1 FROM videos WHERE id=?", (video_id,)).fetchone():
            raise HTTPException(404, "Video not found")
        db.execute(
            "INSERT OR IGNORE INTO video_tags (video_id, tag, owner) VALUES (?,?,?)",
            (video_id, tag, owner),
        )
        db.commit()
    return {"ok": True, "tag": tag}


@router.delete("/api/videos/{video_id}/tags/{tag}")
def remove_video_tag(video_id: str, tag: str, request: Request):
    if not is_authenticated(request):
        raise HTTPException(401)
    owner = current_user(request)
    with get_db() as db:
        db.execute(
            "DELETE FROM video_tags WHERE video_id=? AND tag=? AND owner=?",
            (video_id, tag, owner),
        )
        db.commit()
    return {"ok": True}


@router.patch("/api/videos/{video_id}/note")
def set_video_note(video_id: str, request: Request, body: dict):
    if not is_authenticated(request):
        raise HTTPException(401)
    note = (body.get("note") or "").strip() or None
    with write_lock:
        with get_db() as db:
            db.execute("UPDATE videos SET note=? WHERE id=?", (note, video_id))
            db.commit()
    return {"ok": True, "note": note}


# ── API token helpers ─────────────────────────────────────────────────────────

def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()

def _resolve_api_token(request: Request) -> str | None:
    """Return username if a valid Bearer token is present, else None."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    raw = auth[7:].strip()
    h = _hash_token(raw)
    with get_db() as db:
        row = db.execute(
            "SELECT owner FROM api_tokens WHERE token_hash=?", (h,)
        ).fetchone()
        if row:
            db.execute(
                "UPDATE api_tokens SET last_used_at=? WHERE token_hash=?",
                (now_iso(), h),
            )
            db.commit()
            return row["owner"]
    return None

def _api_auth(request: Request) -> str:
    """Resolve session OR Bearer token; raise 401 if neither."""
    if is_authenticated(request):
        return current_user(request)
    owner = _resolve_api_token(request)
    if owner:
        return owner
    raise HTTPException(401, "Authentication required")


# ── Token management endpoints ────────────────────────────────────────────────

@router.get("/api/user/tokens")
def list_tokens(request: Request):
    if not is_authenticated(request):
        raise HTTPException(401)
    owner = current_user(request)
    with get_db() as db:
        rows = db.execute(
            "SELECT id, label, created_at, last_used_at FROM api_tokens WHERE owner=? ORDER BY created_at DESC",
            (owner,),
        ).fetchall()
    return [dict(r) for r in rows]


@router.post("/api/user/tokens")
def create_token(request: Request, body: dict):
    if not is_authenticated(request):
        raise HTTPException(401)
    owner = current_user(request)
    label = (body.get("label") or "").strip()[:60] or "My token"
    with get_db() as db:
        count = db.execute("SELECT COUNT(*) FROM api_tokens WHERE owner=?", (owner,)).fetchone()[0]
        if count >= 10:
            raise HTTPException(400, "Maximum 10 tokens per account")
    raw = secrets.token_urlsafe(32)
    h = _hash_token(raw)
    with write_lock:
        with get_db() as db:
            db.execute(
                "INSERT INTO api_tokens (owner, token_hash, label, created_at) VALUES (?,?,?,?)",
                (owner, h, label, now_iso()),
            )
            db.commit()
    return {"token": raw, "label": label}


@router.delete("/api/user/tokens/{token_id}")
def delete_token(token_id: int, request: Request):
    if not is_authenticated(request):
        raise HTTPException(401)
    owner = current_user(request)
    with write_lock:
        with get_db() as db:
            db.execute("DELETE FROM api_tokens WHERE id=? AND owner=?", (token_id, owner))
            db.commit()
    return {"ok": True}


# ── Public API endpoints (session OR Bearer token) ────────────────────────────

@router.get("/api/public/videos")
def public_list_videos(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    site_id: str | None = None,
    q: str | None = None,
):
    owner = _api_auth(request)
    conditions = ["s.owner=?"]
    params: list = [owner]
    if site_id:
        conditions.append("v.site_id=?")
        params.append(site_id)
    if q:
        conditions.append("(v.title LIKE ? OR v.cast_names LIKE ?)")
        like = f"%{q}%"
        params += [like, like]
    where = "WHERE " + " AND ".join(conditions)
    offset = (page - 1) * per_page
    with get_db() as db:
        total = db.execute(
            f"SELECT COUNT(*) FROM videos v JOIN sites s ON v.site_id=s.id {where}", params
        ).fetchone()[0]
        rows = db.execute(
            f"SELECT v.id, v.title, v.url, v.thumb, v.platform, v.duration, v.found_at, "
            f"v.released_at, v.cast_names, v.is_favorite, v.is_watched, v.note, "
            f"s.name AS site_name, s.url AS site_url "
            f"FROM videos v JOIN sites s ON v.site_id=s.id {where} "
            f"ORDER BY v.found_at DESC LIMIT ? OFFSET ?",
            params + [per_page, offset],
        ).fetchall()
    return {
        "videos": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, -(-total // per_page)),
    }


@router.post("/api/public/sites")
def public_add_site(request: Request, body: dict):
    """Add a site via Bearer token (used by the browser extension)."""
    owner = _api_auth(request)
    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url is required")
    name = (body.get("name") or "").strip() or None
    scan_interval = int(body.get("scan_interval") or 3600)
    import uuid as _uuid
    site_id = str(_uuid.uuid4())
    with write_lock:
        with get_db() as db:
            existing = db.execute("SELECT id FROM sites WHERE url=? AND owner=?", (url, owner)).fetchone()
            if existing:
                raise HTTPException(409, "Site already exists")
            db.execute(
                "INSERT INTO sites (id, url, name, owner, added_at, scan_interval) VALUES (?,?,?,?,?,?)",
                (site_id, url, name, owner, now_iso(), scan_interval)
            )
            db.commit()
    return {"id": site_id, "url": url, "name": name}


@router.get("/api/public/sites")
def public_list_sites(request: Request):
    owner = _api_auth(request)
    with get_db() as db:
        rows = db.execute(
            "SELECT id, name, url, group_name, last_scan, scan_interval FROM sites WHERE owner=? ORDER BY name",
            (owner,),
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("/api/videos/export.csv")
def export_videos_csv(request: Request):
    if not is_authenticated(request):
        raise HTTPException(401)
    owner = current_user(request)
    with get_db() as db:
        rows = db.execute(
            """
            SELECT v.id, v.title, v.url, v.platform, v.duration, v.found_at, v.released_at,
                   v.cast_names, v.is_new, v.is_favorite, v.is_archived, v.is_ignored,
                   v.is_watched, v.last_watched_at, v.note, s.name AS site_name, s.url AS site_url
            FROM videos v
            JOIN sites s ON v.site_id = s.id
            WHERE s.owner = ?
            ORDER BY v.found_at DESC
            """,
            (owner,),
        ).fetchall()

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "id", "title", "url", "platform", "duration_sec",
            "found_at", "released_at", "cast", "is_new", "is_favorite",
            "is_archived", "is_ignored", "is_watched", "last_watched_at",
            "note", "site_name", "site_url",
        ])
        yield buf.getvalue(); buf.seek(0); buf.truncate()
        for r in rows:
            writer.writerow([
                r["id"], r["title"] or "", r["url"], r["platform"] or "",
                r["duration"] or "", r["found_at"], r["released_at"] or "",
                r["cast_names"] or "", int(r["is_new"] or 0), int(r["is_favorite"] or 0),
                int(r["is_archived"] or 0), int(r["is_ignored"] or 0),
                int(r["is_watched"] or 0), r["last_watched_at"] or "",
                r["note"] or "", r["site_name"] or "", r["site_url"],
            ])
            yield buf.getvalue(); buf.seek(0); buf.truncate()

    filename = f"videowatch-export-{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/videos/duplicates")
def list_duplicate_candidates(limit: int = 50):
    limit = max(10, min(limit, 200))
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT site_id, LOWER(TRIM(title)) as title_key, COALESCE(released_at,'') as rel_key, "
            "COUNT(*) as count "
            "FROM videos "
            "WHERE title IS NOT NULL AND TRIM(title) != '' AND is_ignored=0 "
            "GROUP BY site_id, title_key, rel_key HAVING COUNT(*) > 1 "
            "ORDER BY count DESC LIMIT ?",
            (limit,),
        ).fetchall()]

        result = []
        for g in rows:
            vids = [dict(v) for v in db.execute(
                "SELECT id, site_id, title, url, thumb, found_at, released_at, is_new "
                "FROM videos WHERE site_id=? AND LOWER(TRIM(title))=? AND COALESCE(released_at,'')=? "
                "AND is_ignored=0 "
                "ORDER BY found_at ASC",
                (g["site_id"], g["title_key"], g["rel_key"]),
            ).fetchall()]
            if len(vids) < 2:
                continue
            result.append({
                "site_id": g["site_id"],
                "title_key": g["title_key"],
                "released_at": g["rel_key"] or None,
                "count": g["count"],
                "videos": vids,
            })
    return result


@router.post("/api/videos/duplicates/merge-all")
def merge_all_duplicates(request: Request):
    """Auto-merge all duplicate groups: keep oldest, remove newer copies."""
    require_admin(request)
    merged = 0
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT site_id, LOWER(TRIM(title)) as title_key, COALESCE(released_at,'') as rel_key "
            "FROM videos WHERE title IS NOT NULL AND TRIM(title) != '' AND is_ignored=0 "
            "GROUP BY site_id, title_key, rel_key HAVING COUNT(*) > 1"
        ).fetchall()]
        for g in rows:
            vids = [dict(v) for v in db.execute(
                "SELECT id, thumb, embed_url, cast_names, duration, is_favorite, is_archived, is_ignored "
                "FROM videos WHERE site_id=? AND LOWER(TRIM(title))=? AND COALESCE(released_at,'')=? "
                "AND is_ignored=0 ORDER BY found_at ASC",
                (g["site_id"], g["title_key"], g["rel_key"]),
            ).fetchall()]
            if len(vids) < 2:
                continue
            keep = vids[0]
            for rem in vids[1:]:
                with write_lock:
                    with get_db() as wdb:
                        wdb.execute(
                            "UPDATE videos SET "
                            "thumb=COALESCE(thumb,?), embed_url=COALESCE(embed_url,?), "
                            "cast_names=COALESCE(cast_names,?), duration=COALESCE(duration,?), "
                            "is_favorite=MAX(is_favorite,?), is_archived=MIN(is_archived,?), "
                            "is_ignored=MIN(is_ignored,?) WHERE id=?",
                            (rem["thumb"], rem["embed_url"], rem["cast_names"], rem["duration"],
                             rem["is_favorite"], rem["is_archived"], rem["is_ignored"], keep["id"]),
                        )
                        _delete_thumb(rem.get("thumb"))
                        wdb.execute("DELETE FROM videos WHERE id=?", (rem["id"],))
                        wdb.commit()
                merged += 1
    return {"ok": True, "merged": merged}


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
        new       = db.execute(f"SELECT COUNT(*) {vj} AND videos.is_new=1", op).fetchone()[0]
        favorites = db.execute(f"SELECT COUNT(*) {vj} AND COALESCE(videos.is_favorite,0)=1", op).fetchone()[0]
        archived  = db.execute(f"SELECT COUNT(*) {vj} AND COALESCE(videos.is_archived,0)=1", op).fetchone()[0]
        ignored   = db.execute(f"SELECT COUNT(*) {vj} AND COALESCE(videos.is_ignored,0)=1", op).fetchone()[0]
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


@router.get("/api/admin/analytics")
def admin_analytics(request: Request, days: int = Query(30, ge=7, le=365)):
    if not is_super_admin(request):
        raise HTTPException(403, "Super admin only")
    with get_db() as db:
        # Generate date series for the last N days
        from datetime import timedelta
        today = datetime.now(timezone.utc).date()
        date_list = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]

        # Signups per day
        signup_rows = db.execute(
            "SELECT DATE(created_at) as d, COUNT(*) as c FROM users "
            "WHERE created_at >= ? GROUP BY d",
            ((today - timedelta(days=days)).isoformat(),)
        ).fetchall()
        signup_map = {r["d"]: r["c"] for r in signup_rows}

        # Scans per day
        scan_rows = db.execute(
            "SELECT DATE(scanned_at) as d, COUNT(*) as scans, "
            "SUM(found) as found, SUM(added) as added FROM scan_log "
            "WHERE scanned_at >= ? GROUP BY d",
            ((today - timedelta(days=days)).isoformat(),)
        ).fetchall()
        scan_map  = {r["d"]: r["scans"] for r in scan_rows}
        found_map = {r["d"]: r["found"] or 0 for r in scan_rows}
        added_map = {r["d"]: r["added"] or 0 for r in scan_rows}

        # Cumulative totals
        total_users  = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total_scans  = db.execute("SELECT COUNT(*) FROM scan_log").fetchone()[0]
        total_videos = db.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        total_sites  = db.execute("SELECT COUNT(*) FROM sites").fetchone()[0]

        # Top scanning sites
        top_sites = db.execute(
            "SELECT s.name, s.url, COUNT(sl.id) as scan_count, SUM(sl.added) as videos_added "
            "FROM scan_log sl JOIN sites s ON sl.site_id = s.id "
            "GROUP BY sl.site_id ORDER BY scan_count DESC LIMIT 10"
        ).fetchall()

        # Waitlist signups per day
        waitlist_rows = db.execute(
            "SELECT DATE(created_at) as d, COUNT(*) as c FROM waitlist "
            "WHERE created_at >= ? GROUP BY d",
            ((today - timedelta(days=days)).isoformat(),)
        ).fetchall()
        waitlist_map = {r["d"]: r["c"] for r in waitlist_rows}

    return {
        "dates":         date_list,
        "signups":       [signup_map.get(d, 0)   for d in date_list],
        "scans":         [scan_map.get(d, 0)      for d in date_list],
        "videos_found":  [found_map.get(d, 0)     for d in date_list],
        "videos_added":  [added_map.get(d, 0)     for d in date_list],
        "waitlist":      [waitlist_map.get(d, 0)  for d in date_list],
        "totals": {
            "users":  total_users,
            "scans":  total_scans,
            "videos": total_videos,
            "sites":  total_sites,
        },
        "top_sites": [dict(r) for r in top_sites],
    }


@router.post("/api/waitlist")
def join_waitlist(request: Request, body: dict):
    _check_rate_limit(_client_ip(request), max_attempts=5, window_seconds=3600)
    email = (body.get("email") or "").strip().lower()
    if not email or not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        raise HTTPException(400, "A valid email address is required")
    with write_lock:
        with get_db() as db:
            existing = db.execute("SELECT 1 FROM waitlist WHERE email=?", (email,)).fetchone()
            if existing:
                return {"ok": True, "already_registered": True}
            db.execute(
                "INSERT INTO waitlist (email, source, created_at) VALUES (?,?,?)",
                (email, body.get("source", "mobile"), now_iso()),
            )
            db.commit()
    return {"ok": True, "already_registered": False}


@router.get("/api/admin/waitlist")
def get_waitlist(request: Request):
    if not is_super_admin(request):
        raise HTTPException(403, "Super admin only")
    with get_db() as db:
        rows = db.execute(
            "SELECT id, email, source, created_at FROM waitlist ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@router.delete("/api/admin/waitlist/{entry_id}")
def delete_waitlist_entry(entry_id: int, request: Request):
    if not is_super_admin(request):
        raise HTTPException(403, "Super admin only")
    with write_lock:
        with get_db() as db:
            db.execute("DELETE FROM waitlist WHERE id=?", (entry_id,))
            db.commit()
    return {"ok": True}


@router.post("/api/push/expo-token")
def register_expo_token(body: dict, request: Request):
    """Register an Expo push token for the current user (mobile app)."""
    if not is_authenticated(request):
        raise HTTPException(401)
    token = (body.get("token") or "").strip()
    if not token or not token.startswith("ExponentPushToken["):
        raise HTTPException(400, "Invalid Expo push token")
    owner = current_user(request)
    with write_lock:
        with get_db() as db:
            db.execute(
                "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
                (f"expo_token:{owner}", token)
            )
            db.execute(
                "UPDATE app_settings SET value=? WHERE key=?",
                (token, f"expo_token:{owner}")
            )
            db.commit()
    return {"ok": True}


@router.get("/api/admin/rate-limits")
def admin_rate_limits(request: Request):
    """Return current in-memory rate-limit hit counts per IP."""
    if not is_super_admin(request):
        raise HTTPException(403, "Super admin only")
    now = time.monotonic()
    result = []
    for ip, hits in list(_rate_limit_store.items()):
        recent = [t for t in hits if now - t < 3600]  # last 1h
        if not recent:
            continue
        last_hit = now - max(recent)
        result.append({
            "ip": ip,
            "hits_1h": len(recent),
            "last_hit_seconds_ago": round(last_hit),
        })
    result.sort(key=lambda x: x["hits_1h"], reverse=True)
    return {"entries": result}


@router.delete("/api/admin/rate-limits/{ip}")
def admin_clear_rate_limit(ip: str, request: Request):
    """Clear rate-limit record for a specific IP."""
    if not is_super_admin(request):
        raise HTTPException(403, "Super admin only")
    _rate_limit_store.pop(ip, None)
    return {"ok": True}


@router.post("/api/admin/send-digest")
def trigger_digest(request: Request):
    if not is_super_admin(request):
        raise HTTPException(403, "Super admin only")
    threading.Thread(target=send_weekly_digest, daemon=True).start()
    return {"ok": True, "message": "Weekly digest queued"}


@router.post("/api/admin/broadcast")
def admin_broadcast(request: Request, body: dict):
    """Send an email announcement to all verified users (or optionally to waitlist too)."""
    if not is_super_admin(request):
        raise HTTPException(403, "Super admin only")
    subject = (body.get("subject") or "").strip()
    html_body = (body.get("html") or "").strip()
    text_body = (body.get("text") or "").strip()
    include_waitlist = bool(body.get("include_waitlist", False))
    if not subject or not html_body:
        raise HTTPException(400, "subject and html are required")

    with get_db() as db:
        user_rows = db.execute(
            "SELECT email FROM users WHERE email IS NOT NULL AND email_verified=1"
        ).fetchall()
        waitlist_rows = db.execute("SELECT email FROM waitlist").fetchall() if include_waitlist else []

    recipients = list({r["email"] for r in user_rows if r["email"]} |
                      {r["email"] for r in waitlist_rows if r["email"]})

    def _blast():
        sent, failed = 0, 0
        for addr in recipients:
            try:
                _send_email_simple(addr, subject, html_body, text_body)
                sent += 1
            except Exception:
                failed += 1
        log.info(f"Broadcast '{subject}': sent={sent} failed={failed}")

    threading.Thread(target=_blast, daemon=True).start()
    return {"ok": True, "queued": len(recipients)}


def _get_vapid_keys():
    """Return (private_key, public_key) VAPID strings, generating them once if missing."""
    with get_db() as db:
        priv = db.execute("SELECT value FROM app_settings WHERE key='vapid_private'").fetchone()
        pub  = db.execute("SELECT value FROM app_settings WHERE key='vapid_public'").fetchone()
    if priv and pub:
        return priv["value"], pub["value"]
    try:
        from py_vapid import Vapid
        v = Vapid()
        v.generate_keys()
        priv_pem = v.private_pem().decode() if isinstance(v.private_pem(), bytes) else v.private_pem()
        pub_b64  = v.public_key_urlsafe_base64
        with write_lock:
            with get_db() as db:
                db.execute("INSERT OR IGNORE INTO app_settings (key,value) VALUES ('vapid_private',?)", (priv_pem,))
                db.execute("INSERT OR IGNORE INTO app_settings (key,value) VALUES ('vapid_public',?)", (pub_b64,))
                db.commit()
        return priv_pem, pub_b64
    except Exception as e:
        log.warning(f"VAPID key generation failed: {e}")
        return None, None


def _send_push(subscription: dict, payload: dict):
    """Send a single Web Push notification. Call in a thread."""
    try:
        from pywebpush import webpush, WebPushException
        import json
        _, _ = _get_vapid_keys()
        with get_db() as db:
            priv = db.execute("SELECT value FROM app_settings WHERE key='vapid_private'").fetchone()
        if not priv:
            return
        webpush(
            subscription_info={
                "endpoint": subscription["endpoint"],
                "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
            },
            data=json.dumps(payload),
            vapid_private_key=priv["value"],
            vapid_claims={"sub": "mailto:admin@videowatch.duckdns.org"},
        )
    except Exception as e:
        log.warning(f"Push send failed: {e}")


@router.get("/api/push/vapid-public")
def push_vapid_public():
    _, pub = _get_vapid_keys()
    if not pub:
        raise HTTPException(503, "Push notifications not configured")
    return {"public_key": pub}


@router.post("/api/push/subscribe")
def push_subscribe(request: Request, body: dict):
    username = _api_auth(request)
    endpoint = body.get("endpoint", "").strip()
    p256dh   = body.get("p256dh", "").strip()
    auth     = body.get("auth", "").strip()
    if not endpoint or not p256dh or not auth:
        raise HTTPException(400, "endpoint, p256dh and auth required")
    with write_lock:
        with get_db() as db:
            db.execute(
                "INSERT OR REPLACE INTO push_subscriptions (owner, endpoint, p256dh, auth, created_at) VALUES (?,?,?,?,?)",
                (username, endpoint, p256dh, auth, now_iso())
            )
            db.commit()
    return {"ok": True}


@router.delete("/api/push/subscribe")
def push_unsubscribe(request: Request, body: dict):
    username = _api_auth(request)
    endpoint = body.get("endpoint", "").strip()
    with write_lock:
        with get_db() as db:
            db.execute("DELETE FROM push_subscriptions WHERE owner=? AND endpoint=?", (username, endpoint))
            db.commit()
    return {"ok": True}


def _push_new_videos(owner: str, site_name: str, added: int, url: str):
    """Send push to all of a user's subscribed devices when new videos are found."""
    try:
        from pywebpush import webpush  # noqa — just check import is available
    except ImportError:
        return
    with get_db() as db:
        subs = db.execute("SELECT * FROM push_subscriptions WHERE owner=?", (owner,)).fetchall()
    payload = {
        "title": f"New video on {site_name}",
        "body": f"{added} new video{'s' if added != 1 else ''} found",
        "url": url,
        "tag": f"vw-site-{owner}",
    }
    for sub in subs:
        threading.Thread(target=_send_push, args=(dict(sub), payload), daemon=True).start()


@router.get("/api/user/referral")
def get_referral(request: Request):
    username = _api_auth(request)
    with get_db() as db:
        row = db.execute("SELECT referral_code, referred_by FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            raise HTTPException(404)
        # Ensure referral code exists
        ref_code = row["referral_code"]
        if not ref_code:
            ref_code = secrets.token_urlsafe(8)
            with write_lock:
                with get_db() as db2:
                    db2.execute("UPDATE users SET referral_code=? WHERE username=?", (ref_code, username))
                    db2.commit()
        count = db.execute("SELECT COUNT(*) FROM users WHERE referred_by=?", (username,)).fetchone()[0]
        referrals = db.execute(
            "SELECT username, created_at FROM users WHERE referred_by=? ORDER BY created_at DESC LIMIT 50",
            (username,)
        ).fetchall()
    base = "https://videowatch.duckdns.org"
    return {
        "referral_code": ref_code,
        "referral_url": f"{base}/register?ref={ref_code}",
        "referred_by": row["referred_by"],
        "count": count,
        "referrals": [{"username": r["username"], "joined": r["created_at"][:10]} for r in referrals],
    }


@router.get("/api/roadmap")
def get_roadmap(request: Request):
    """Public endpoint — returns all roadmap items with vote counts."""
    username = None
    try:
        username = current_user(request)
    except Exception:
        pass
    with get_db() as db:
        rows = db.execute(
            """SELECT r.id, r.title, r.description, r.status, r.sort_order,
                      COUNT(v.username) AS votes,
                      MAX(CASE WHEN v.username=? THEN 1 ELSE 0 END) AS voted
               FROM roadmap_items r
               LEFT JOIN roadmap_votes v ON v.item_id=r.id
               GROUP BY r.id ORDER BY r.status, votes DESC, r.sort_order""",
            (username or "",)
        ).fetchall()
    return [dict(r) for r in rows]


@router.post("/api/roadmap/vote/{item_id}")
def vote_roadmap(item_id: str, request: Request):
    username = _api_auth(request)
    with write_lock:
        with get_db() as db:
            if not db.execute("SELECT 1 FROM roadmap_items WHERE id=?", (item_id,)).fetchone():
                raise HTTPException(404)
            existing = db.execute("SELECT 1 FROM roadmap_votes WHERE item_id=? AND username=?", (item_id, username)).fetchone()
            if existing:
                db.execute("DELETE FROM roadmap_votes WHERE item_id=? AND username=?", (item_id, username))
                voted = False
            else:
                db.execute("INSERT INTO roadmap_votes (item_id, username) VALUES (?,?)", (item_id, username))
                voted = True
            count = db.execute("SELECT COUNT(*) FROM roadmap_votes WHERE item_id=?", (item_id,)).fetchone()[0]
            db.commit()
    return {"ok": True, "voted": voted, "votes": count}


@router.get("/api/admin/roadmap")
def admin_get_roadmap(request: Request):
    if not is_admin(request):
        raise HTTPException(403)
    with get_db() as db:
        rows = db.execute(
            """SELECT r.id, r.title, r.description, r.status, r.sort_order, COUNT(v.username) AS votes
               FROM roadmap_items r LEFT JOIN roadmap_votes v ON v.item_id=r.id
               GROUP BY r.id ORDER BY r.sort_order, r.created_at"""
        ).fetchall()
    return [dict(r) for r in rows]


@router.post("/api/admin/roadmap")
def admin_create_roadmap(request: Request, body: dict):
    if not is_admin(request):
        raise HTTPException(403)
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "title required")
    rid = str(__import__("uuid").uuid4())
    with write_lock:
        with get_db() as db:
            db.execute(
                "INSERT INTO roadmap_items (id, title, description, status, sort_order, created_at) VALUES (?,?,?,?,?,?)",
                (rid, title, body.get("description",""), body.get("status","planned"), int(body.get("sort_order",0)), now_iso())
            )
            db.commit()
    return {"id": rid, "title": title, "votes": 0}


@router.patch("/api/admin/roadmap/{item_id}")
def admin_update_roadmap(item_id: str, request: Request, body: dict):
    if not is_admin(request):
        raise HTTPException(403)
    fields = {}
    if "title" in body: fields["title"] = body["title"]
    if "description" in body: fields["description"] = body["description"]
    if "status" in body: fields["status"] = body["status"]
    if "sort_order" in body: fields["sort_order"] = int(body["sort_order"])
    if not fields:
        raise HTTPException(400, "nothing to update")
    set_clause = ", ".join(f"{k}=?" for k in fields)
    with write_lock:
        with get_db() as db:
            db.execute(f"UPDATE roadmap_items SET {set_clause} WHERE id=?", [*fields.values(), item_id])
            db.commit()
    return {"ok": True}


@router.delete("/api/admin/roadmap/{item_id}")
def admin_delete_roadmap(item_id: str, request: Request):
    if not is_admin(request):
        raise HTTPException(403)
    with write_lock:
        with get_db() as db:
            db.execute("DELETE FROM roadmap_items WHERE id=?", (item_id,))
            db.commit()
    return {"ok": True}


@router.get("/api/videos/{video_id}/collections")
def video_collections(video_id: str, request: Request):
    """Return which collections this video belongs to (for the current user)."""
    username = _api_auth(request)
    with get_db() as db:
        rows = db.execute(
            """SELECT c.id, c.name FROM collections c
               JOIN collection_videos cv ON cv.collection_id=c.id
               WHERE cv.video_id=? AND c.owner=?""",
            (video_id, username)
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("/api/collections")
def list_collections(request: Request):
    username = _api_auth(request)
    with get_db() as db:
        rows = db.execute(
            """SELECT c.id, c.name, c.created_at, COUNT(cv.video_id) AS video_count
               FROM collections c
               LEFT JOIN collection_videos cv ON cv.collection_id=c.id
               WHERE c.owner=? GROUP BY c.id ORDER BY c.created_at DESC""",
            (username,)
        ).fetchall()
    return [dict(r) for r in rows]


@router.post("/api/collections")
def create_collection(request: Request, body: dict):
    username = _api_auth(request)
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    cid = str(__import__("uuid").uuid4())
    with write_lock:
        with get_db() as db:
            db.execute(
                "INSERT INTO collections (id, owner, name, created_at) VALUES (?,?,?,?)",
                (cid, username, name, now_iso())
            )
            db.commit()
    return {"id": cid, "name": name, "video_count": 0}


@router.delete("/api/collections/{cid}")
def delete_collection(cid: str, request: Request):
    username = _api_auth(request)
    with write_lock:
        with get_db() as db:
            row = db.execute("SELECT owner FROM collections WHERE id=?", (cid,)).fetchone()
            if not row:
                raise HTTPException(404)
            if row["owner"] != username:
                raise HTTPException(403)
            db.execute("DELETE FROM collections WHERE id=?", (cid,))
            db.commit()
    return {"ok": True}


@router.patch("/api/collections/{cid}")
def rename_collection(cid: str, request: Request, body: dict):
    username = _api_auth(request)
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    with write_lock:
        with get_db() as db:
            row = db.execute("SELECT owner FROM collections WHERE id=?", (cid,)).fetchone()
            if not row:
                raise HTTPException(404)
            if row["owner"] != username:
                raise HTTPException(403)
            db.execute("UPDATE collections SET name=? WHERE id=?", (name, cid))
            db.commit()
    return {"ok": True}


@router.get("/api/collections/{cid}/videos")
def collection_videos(cid: str, request: Request):
    username = _api_auth(request)
    with get_db() as db:
        col = db.execute("SELECT owner FROM collections WHERE id=?", (cid,)).fetchone()
        if not col or col["owner"] != username:
            raise HTTPException(404)
        rows = db.execute(
            """SELECT v.id, v.title, v.url, v.thumb, v.platform, v.duration,
                      v.is_favorite, v.is_watched, v.note, s.name AS site_name
               FROM collection_videos cv
               JOIN videos v ON v.id=cv.video_id
               JOIN sites s ON s.id=v.site_id
               WHERE cv.collection_id=?
               ORDER BY cv.added_at DESC""",
            (cid,)
        ).fetchall()
    return [dict(r) for r in rows]


@router.post("/api/collections/{cid}/videos")
def add_to_collection(cid: str, request: Request, body: dict):
    username = _api_auth(request)
    video_id = (body.get("video_id") or "").strip()
    if not video_id:
        raise HTTPException(400, "video_id required")
    with write_lock:
        with get_db() as db:
            col = db.execute("SELECT owner FROM collections WHERE id=?", (cid,)).fetchone()
            if not col or col["owner"] != username:
                raise HTTPException(404)
            db.execute(
                "INSERT OR IGNORE INTO collection_videos (collection_id, video_id, added_at) VALUES (?,?,?)",
                (cid, video_id, now_iso())
            )
            db.commit()
    return {"ok": True}


@router.delete("/api/collections/{cid}/videos/{video_id}")
def remove_from_collection(cid: str, video_id: str, request: Request):
    username = _api_auth(request)
    with write_lock:
        with get_db() as db:
            col = db.execute("SELECT owner FROM collections WHERE id=?", (cid,)).fetchone()
            if not col or col["owner"] != username:
                raise HTTPException(404)
            db.execute("DELETE FROM collection_videos WHERE collection_id=? AND video_id=?", (cid, video_id))
            db.commit()
    return {"ok": True}


@router.post("/api/collections/{cid}/share")
def generate_share_link(cid: str, request: Request):
    """Generate (or return existing) a public share token for a collection."""
    username = _api_auth(request)
    with write_lock:
        with get_db() as db:
            col = db.execute("SELECT * FROM collections WHERE id=?", (cid,)).fetchone()
            if not col or col["owner"] != username:
                raise HTTPException(404)
            token = col["share_token"]
            if not token:
                import secrets
                token = secrets.token_urlsafe(16)
                db.execute("UPDATE collections SET share_token=? WHERE id=?", (token, cid))
                db.commit()
    return {"token": token, "url": f"/shared/collection/{token}"}


@router.delete("/api/collections/{cid}/share")
def revoke_share_link(cid: str, request: Request):
    """Revoke the public share link for a collection."""
    username = _api_auth(request)
    with write_lock:
        with get_db() as db:
            col = db.execute("SELECT owner FROM collections WHERE id=?", (cid,)).fetchone()
            if not col or col["owner"] != username:
                raise HTTPException(404)
            db.execute("UPDATE collections SET share_token=NULL WHERE id=?", (cid,))
            db.commit()
    return {"ok": True}


@router.get("/api/shared/collection/{token}")
def public_collection(token: str):
    """Public endpoint — returns collection info + videos for a share token."""
    with get_db() as db:
        col = db.execute("SELECT * FROM collections WHERE share_token=?", (token,)).fetchone()
        if not col:
            raise HTTPException(404, "Collection not found or link has been revoked")
        videos = [dict(r) for r in db.execute(
            """SELECT v.id, v.title, v.url, v.thumb, v.duration, v.platform, v.released_at
               FROM videos v
               JOIN collection_videos cv ON cv.video_id=v.id
               WHERE cv.collection_id=?
               ORDER BY cv.added_at DESC""",
            (col["id"],)
        ).fetchall()]
    return {"name": col["name"], "description": col.get("description"), "video_count": len(videos), "videos": videos}


@router.get("/api/videos/history")
def watch_history(request: Request, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    """Return videos the current user has marked as watched, newest-watched first."""
    username = _api_auth(request)
    with get_db() as db:
        total = db.execute(
            "SELECT COUNT(*) FROM videos v JOIN sites s ON s.id=v.site_id WHERE s.owner=? AND v.is_watched=1",
            (username,)
        ).fetchone()[0]
        rows = db.execute(
            """SELECT v.id, v.title, v.url, v.thumb, v.platform, v.duration,
                      v.last_watched_at, v.is_favorite, v.note,
                      s.name AS site_name, s.url AS site_url
               FROM videos v JOIN sites s ON s.id=v.site_id
               WHERE s.owner=? AND v.is_watched=1
               ORDER BY v.last_watched_at DESC NULLS LAST
               LIMIT ? OFFSET ?""",
            (username, limit, offset)
        ).fetchall()
    return {"total": total, "items": [dict(r) for r in rows]}


def _run_backup() -> dict:
    """Copy the SQLite DB to backups/ with a timestamp name; prune old copies."""
    from db import DB_PATH
    import sqlite3
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = BACKUPS_DIR / f"videowatch_{ts}.db"
    # Use SQLite's online backup API so the copy is consistent even under writes
    src_conn = sqlite3.connect(DB_PATH, timeout=30)
    dst_conn = sqlite3.connect(str(dest))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    size = dest.stat().st_size
    # Prune oldest beyond keep limit
    backups = sorted(BACKUPS_DIR.glob("videowatch_*.db"), key=lambda p: p.stat().st_mtime)
    for old in backups[:-_BACKUP_KEEP]:
        old.unlink(missing_ok=True)
    log.info(f"DB backup created: {dest.name} ({size} bytes)")
    return {"filename": dest.name, "size": size, "created_at": ts}


@router.post("/api/admin/backup")
def trigger_backup(request: Request):
    if not is_super_admin(request):
        raise HTTPException(403, "Super admin only")
    result = _run_backup()
    return {"ok": True, **result}


@router.get("/api/admin/backups")
def list_backups(request: Request):
    if not is_super_admin(request):
        raise HTTPException(403, "Super admin only")
    files = sorted(BACKUPS_DIR.glob("videowatch_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        {"filename": p.name, "size": p.stat().st_size, "created_at": p.stem.replace("videowatch_", "")}
        for p in files
    ]


@router.get("/api/admin/backups/{filename}")
def download_backup(filename: str, request: Request):
    if not is_super_admin(request):
        raise HTTPException(403, "Super admin only")
    # Sanitise to prevent path traversal
    safe = Path(filename).name
    if not safe.startswith("videowatch_") or not safe.endswith(".db"):
        raise HTTPException(400, "Invalid filename")
    path = BACKUPS_DIR / safe
    if not path.exists():
        raise HTTPException(404, "Backup not found")
    from fastapi.responses import FileResponse as FR
    return FR(str(path), filename=safe, media_type="application/octet-stream")


@router.get("/api/pwa/icon")
def pwa_icon(size: int = 192):
    """Generate a simple PNG icon for the PWA manifest."""
    try:
        from PIL import Image, ImageDraw
        import io
        size = max(16, min(size, 1024))
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        # Rounded background
        pad = size // 8
        draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=size // 5, fill="#0f766e")
        # Simple "V" play shape
        cx, cy = size // 2, size // 2
        r = size // 3
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill="#ffffff")
        tri = [(cx - r // 2, cy - r // 2), (cx - r // 2, cy + r // 2), (cx + r // 2, cy)]
        draw.polygon(tri, fill="#0f766e")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        from fastapi.responses import Response
        return Response(content=buf.read(), media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})
    except ImportError:
        # Pillow not installed — redirect to SVG
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/static/og-image.svg")


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


# ── Video Downloads (yt-dlp) ───────────────────────────────────────────────────

import subprocess
import uuid as _uuid

_dl_lock   = threading.Lock()
_dl_queue: queue.Queue = queue.Queue()
_dl_active: dict = {}   # download_id -> True while running

_YOUTUBE_DOMAINS = {"youtube.com", "youtu.be", "www.youtube.com", "m.youtube.com"}


def _is_youtube(url: str) -> bool:
    try:
        return urlparse(url).netloc.lstrip("www.") in _YOUTUBE_DOMAINS
    except Exception:
        return False


def _dl_worker():
    """Single background thread that processes the download queue."""
    while True:
        dl_id = _dl_queue.get()
        try:
            with get_db() as db:
                row = db.execute("SELECT * FROM downloads WHERE id=?", (dl_id,)).fetchone()
            if not row:
                continue

            video_id = row["video_id"]
            with get_db() as db:
                vrow = db.execute("SELECT url, title FROM videos WHERE id=?", (video_id,)).fetchone()
            if not vrow:
                _dl_set_status(dl_id, "failed", error="Video not found")
                continue

            url   = vrow["url"]
            title = vrow["title"] or dl_id
            safe  = re.sub(r'[^\w\-]', '_', title)[:60]
            out   = DOWNLOADS_DIR / f"{dl_id}_{safe}.%(ext)s"

            _dl_set_status(dl_id, "downloading", progress=0)

            cmd = [
                "yt-dlp",
                "--no-playlist",
                "--merge-output-format", "mp4",
                "--output", str(out),
                "--progress",
                "--newline",
                url,
            ]

            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
            _dl_active[dl_id] = proc

            for line in proc.stdout:
                line = line.strip()
                # Parse progress lines: [download]  45.3% ...
                m = re.search(r'\[download\]\s+([\d.]+)%', line)
                if m:
                    pct = int(float(m.group(1)))
                    _dl_set_status(dl_id, "downloading", progress=pct)

            proc.wait()
            _dl_active.pop(dl_id, None)

            if proc.returncode != 0:
                _dl_set_status(dl_id, "failed", error="yt-dlp exited with errors")
                continue

            # Find the output file (yt-dlp fills in the extension)
            matches = list(DOWNLOADS_DIR.glob(f"{dl_id}_*.mp4")) + \
                      list(DOWNLOADS_DIR.glob(f"{dl_id}_*"))
            if not matches:
                _dl_set_status(dl_id, "failed", error="Output file not found")
                continue

            fp = matches[0]
            size = fp.stat().st_size
            with write_lock:
                with get_db() as db:
                    db.execute(
                        "UPDATE downloads SET status='done', progress=100, file_path=?, "
                        "file_size=?, updated_at=? WHERE id=?",
                        (fp.name, size, datetime.now(timezone.utc).isoformat(), dl_id),
                    )
                    db.commit()
        except Exception as e:
            log.exception(f"Download worker error for {dl_id}: {e}")
            _dl_set_status(dl_id, "failed", error=str(e))
        finally:
            _dl_queue.task_done()


def _dl_set_status(dl_id: str, status: str, progress: int = None, error: str = None):
    with write_lock:
        with get_db() as db:
            db.execute(
                "UPDATE downloads SET status=?, progress=COALESCE(?,progress), "
                "error=?, updated_at=? WHERE id=?",
                (status, progress, error, datetime.now(timezone.utc).isoformat(), dl_id),
            )
            db.commit()


# Start worker thread once
_dl_thread = threading.Thread(target=_dl_worker, daemon=True, name="dl-worker")
_dl_thread.start()


@router.post("/api/downloads")
def start_download(request: Request, body: dict):
    if not is_authenticated(request):
        raise HTTPException(401)
    video_id = body.get("video_id", "")
    if not video_id:
        raise HTTPException(400, "video_id required")

    with get_db() as db:
        vrow = db.execute("SELECT url FROM videos WHERE id=?", (video_id,)).fetchone()
    if not vrow:
        raise HTTPException(404, "Video not found")

    # Warn but still allow YouTube (user's choice)
    owner = request.session.get("auth_user", "")
    dl_id = str(_uuid.uuid4())
    now   = datetime.now(timezone.utc).isoformat()
    with write_lock:
        with get_db() as db:
            # Cancel existing queued/failed downloads for the same video+owner
            db.execute(
                "DELETE FROM downloads WHERE video_id=? AND owner=? AND status IN ('queued','failed')",
                (video_id, owner),
            )
            db.execute(
                "INSERT INTO downloads (id, video_id, owner, status, progress, created_at, updated_at) "
                "VALUES (?,?,?,'queued',0,?,?)",
                (dl_id, video_id, owner, now, now),
            )
            db.commit()

    _dl_queue.put(dl_id)
    return {"id": dl_id, "status": "queued"}


@router.get("/api/downloads")
def list_downloads(request: Request):
    if not is_authenticated(request):
        raise HTTPException(401)
    owner = request.session.get("auth_user", "")
    with get_db() as db:
        rows = db.execute("""
            SELECT d.*, v.title, v.thumb, v.url as video_url
            FROM downloads d
            JOIN videos v ON v.id = d.video_id
            WHERE d.owner=?
            ORDER BY d.created_at DESC
            LIMIT 100
        """, (owner,)).fetchall()
    return [dict(r) for r in rows]


@router.get("/api/downloads/{dl_id}")
def get_download(request: Request, dl_id: str):
    if not is_authenticated(request):
        raise HTTPException(401)
    owner = request.session.get("auth_user", "")
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM downloads WHERE id=? AND owner=?", (dl_id, owner)
        ).fetchone()
    if not row:
        raise HTTPException(404)
    return dict(row)


@router.delete("/api/downloads/{dl_id}")
def delete_download(request: Request, dl_id: str):
    if not is_authenticated(request):
        raise HTTPException(401)
    owner = request.session.get("auth_user", "")
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM downloads WHERE id=? AND owner=?", (dl_id, owner)
        ).fetchone()
    if not row:
        raise HTTPException(404)

    # Kill process if running
    proc = _dl_active.pop(dl_id, None)
    if proc:
        try:
            proc.terminate()
        except Exception:
            pass

    # Delete file
    if row["file_path"]:
        fp = DOWNLOADS_DIR / row["file_path"]
        if fp.exists():
            fp.unlink(missing_ok=True)

    with write_lock:
        with get_db() as db:
            db.execute("DELETE FROM downloads WHERE id=?", (dl_id,))
            db.commit()
    return {"ok": True}


@router.get("/api/downloads/{dl_id}/file")
def serve_download(request: Request, dl_id: str):
    """Stream the downloaded file to the browser."""
    if not is_authenticated(request):
        raise HTTPException(401)
    owner = request.session.get("auth_user", "")
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM downloads WHERE id=? AND owner=? AND status='done'",
            (dl_id, owner),
        ).fetchone()
    if not row or not row["file_path"]:
        raise HTTPException(404)
    fp = DOWNLOADS_DIR / row["file_path"]
    if not fp.exists():
        raise HTTPException(404, "File not found on disk")

    from fastapi.responses import FileResponse
    return FileResponse(
        path=str(fp),
        media_type="video/mp4",
        filename=fp.name,
    )
