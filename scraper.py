import re
import os
import sys
import json
import hashlib
import logging
import asyncio
import warnings
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs, urlunparse, urldefrag

import httpx
from bs4 import BeautifulSoup
from playwright_stealth import Stealth

try:
    from curl_cffi.requests import AsyncSession as CurlSession
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False
    logging.warning("curl_cffi not installed — Cloudflare bypass unavailable")

from playwright.async_api import async_playwright, BrowserContext
from playwright.sync_api import sync_playwright

from db import get_db, write_lock

# ── Paths & Logging ───────────────────────────────────────────────────────────
COOKIES_DIR = Path("cookies")
COOKIES_DIR.mkdir(exist_ok=True)
log = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────

def short_id(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()[:12]

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def normalize_url(url: str) -> str:
    """Strip fragments, sort query params, remove common tracking params."""
    STRIP_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_content",
                    "utm_term", "ref", "source", "fbclid", "gclid"}
    url, _ = urldefrag(url)
    p = urlparse(url)
    qs = parse_qs(p.query, keep_blank_values=False)
    qs = {k: v for k, v in qs.items() if k.lower() not in STRIP_PARAMS}
    clean_qs = "&".join(f"{k}={v[0]}" for k, v in sorted(qs.items()))
    path = p.path.rstrip("/") or "/"
    return urlunparse(p._replace(query=clean_qs, path=path, fragment=""))

def page_url(base: str, page_num: int) -> str:
    if page_num == 1:
        return base
    parsed = urlparse(base)
    if re.search(r'/(page|p)/\d+', parsed.path):
        new_path = re.sub(r'/(page|p)/\d+', f'/page/{page_num}', parsed.path)
        return urlunparse(parsed._replace(path=new_path))
    qs = parse_qs(parsed.query)
    qs["page"] = [str(page_num)]
    flat_qs = "&".join(f"{k}={v[0]}" for k, v in qs.items())
    return urlunparse(parsed._replace(query=flat_qs))

def normalize_title(title: str) -> str:
    """Normalize a video title so every word starts with a capital letter."""
    title = re.sub(r'-+', ' ', (title or '').strip())
    title = re.sub(r'\s+', ' ', title)
    return title.title()


def _split_keywords(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [t.strip().lower() for t in re.split(r"[,\n;]+", raw) if t.strip()]


def _effective_max_pages(site: dict) -> int:
    base = int(site.get("max_pages") or 1)
    profile = (site.get("scan_profile") or "balanced").strip().lower()
    if profile == "fast":
        return 1
    if profile == "deep":
        return max(1, min(20, base * 2))
    return max(1, min(20, base))


def _apply_site_rules(site: dict, videos: list[dict]) -> list[dict]:
    includes = _split_keywords(site.get("rule_include_keywords"))
    excludes = _split_keywords(site.get("rule_exclude_keywords"))
    min_duration = int(site.get("rule_min_duration") or 0)

    if not includes and not excludes and min_duration <= 0:
        return videos

    out: list[dict] = []
    for v in videos:
        text = " ".join([
            (v.get("title") or ""),
            (v.get("cast_names") or ""),
            (v.get("url") or ""),
        ]).lower()

        if includes and not any(k in text for k in includes):
            continue
        if excludes and any(k in text for k in excludes):
            continue
        if min_duration > 0 and (v.get("duration") or 0) < min_duration:
            continue
        out.append(v)
    return out


def _send_scan_notification(site: dict, found: int, added: int):
    if added <= 0:
        return
    try:
        with get_db() as db:
            enabled_row = db.execute("SELECT value FROM app_settings WHERE key='notify_enabled'").fetchone()
            webhook_row = db.execute("SELECT value FROM app_settings WHERE key='notify_webhook_url'").fetchone()
        enabled = bool(enabled_row and enabled_row["value"] == "1")
        webhook = (webhook_row["value"] if webhook_row else "") or ""
        webhook = webhook.strip()
        if not enabled or not webhook:
            return
        payload = {
            "text": f"VideoWatch: {(site.get('name') or site.get('url') or site.get('id'))} scan complete - {added} new, {found} found.",
            "site_id": site.get("id"),
            "site": site.get("name") or site.get("url") or site.get("id"),
            "found": found,
            "added": added,
            "time": now_iso(),
        }
        httpx.post(webhook, json=payload, timeout=8.0)
    except Exception as e:
        log.warning(f"Notification webhook failed: {e}")

def cookie_path(site_id: str) -> Path:
    return COOKIES_DIR / f"{site_id}.json"

def parse_release_date(val) -> str | None:
    """
    Parse a release date from various API formats and return a normalised
    'YYYY-MM-DDTHH:MM:SS' string (no timezone suffix) so SQLite text-sort
    always works correctly.
    """
    if not val:
        return None
    dt = None
    if isinstance(val, (int, float)):
        try:
            dt = datetime.fromtimestamp(val, tz=timezone.utc)
        except Exception:
            return None
    elif isinstance(val, str):
        clean = re.sub(r"\s+", " ", val.strip())

        rel = clean.lower()
        if rel in {"just now", "now", "today"}:
            dt = datetime.now(timezone.utc)
        elif rel == "yesterday":
            dt = datetime.now(timezone.utc) - timedelta(days=1)
        else:
            m = re.search(
                r"(\d+)\s*(minute|hour|day|week|month|year)s?\s+ago",
                rel,
            )
            if m:
                qty = int(m.group(1))
                unit = m.group(2)
                scale = {
                    "minute": timedelta(minutes=qty),
                    "hour": timedelta(hours=qty),
                    "day": timedelta(days=qty),
                    "week": timedelta(weeks=qty),
                    "month": timedelta(days=30 * qty),
                    "year": timedelta(days=365 * qty),
                }
                dt = datetime.now(timezone.utc) - scale[unit]

        for fmt in (
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%d",
            "%d/%m/%Y",
            "%m/%d/%Y",
        ):
            try:
                dt = datetime.strptime(clean[:26], fmt)
                break
            except Exception:
                continue
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")

# ── Platform Detection ────────────────────────────────────────────────────────

VIDEO_EXTENSIONS = re.compile(r'\.(mp4|webm|ogg|mov|avi|mkv|m3u8)(\?|$)', re.I)
YOUTUBE_PATTERNS = [
    re.compile(r'youtube\.com/watch\?v=([\w-]+)'),
    re.compile(r'youtu\.be/([\w-]+)'),
    re.compile(r'youtube\.com/embed/([\w-]+)'),
    re.compile(r'youtube\.com/shorts/([\w-]+)'),
]
VIMEO_PAT = re.compile(r'vimeo\.com/(?:video/)?(\d+)')
TWITCH_PAT = re.compile(r'twitch\.tv/videos/(\d+)')
DAILYMOTION_PAT = re.compile(r'dailymotion\.com/video/([\w]+)')

def detect_platform(url: str):
    for pat in YOUTUBE_PATTERNS:
        m = pat.search(url)
        if m:
            vid = m.group(1)
            return ("youtube", f"https://www.youtube.com/watch?v={vid}",
                    f"https://img.youtube.com/vi/{vid}/hqdefault.jpg",
                    f"https://www.youtube.com/embed/{vid}")
    m = VIMEO_PAT.search(url)
    if m:
        vid = m.group(1)
        return ("vimeo", f"https://vimeo.com/{vid}", None,
                f"https://player.vimeo.com/video/{vid}")
    m = TWITCH_PAT.search(url)
    if m:
        vid = m.group(1)
        return ("twitch", f"https://www.twitch.tv/videos/{vid}", None,
                f"https://player.twitch.tv/?video={vid}&parent=localhost")
    m = DAILYMOTION_PAT.search(url)
    if m:
        vid = m.group(1)
        return ("dailymotion", f"https://www.dailymotion.com/video/{vid}",
                f"https://www.dailymotion.com/thumbnail/video/{vid}",
                f"https://www.dailymotion.com/embed/video/{vid}")
    if VIDEO_EXTENSIONS.search(url):
        return ("direct", url, None, url)
    
    url_lower = url.lower()
    parsed_path = urlparse(url).path.rstrip("/")
    path_segments = [s for s in parsed_path.split("/") if s]

    VIDEO_PATH_KEYWORDS = (
        "/video/", "/videos/", "/scene/", "/scenes/",
        "/movie/", "/movies/", "/episode/", "/episodes/",
        "/content/", "/watch/", "/clip/", "/clips/",
        "/stream/", "/embed/", "/play/",
        "/v/", "/porn/", "/hd/", "/xxx/", "/categories/",
    )
    if any(kw in url_lower for kw in VIDEO_PATH_KEYWORDS) and len(path_segments) >= 2:
        return ("direct", url, None, None)

    LISTING_SEGMENTS = {"videos", "video", "scenes", "scene", "movies", "movie",
                        "episodes", "episode", "clips", "clip", "models", "model",
                        "pornstars", "pornstar", "categories", "category",
                        "tags", "tag", "channels", "channel", "studios", "studio"}
    if len(path_segments) >= 3:
        last_seg = path_segments[-1]
        if re.search(r'[a-zA-Z]{3,}', last_seg) and last_seg not in LISTING_SEGMENTS:
            return ("direct", url, None, None)

    if "/channels/" in url_lower:
        return ("direct", url, None, None)
    return None

def extract_title(soup: BeautifulSoup, fallback: str = "") -> str:
    for sel in ["h1", "h2", "title", "meta[property='og:title']", "meta[name='title']"]:
        el = soup.select_one(sel)
        if el:
            return normalize_title((el.get("content") or el.get_text()).strip()[:200])
    return normalize_title(fallback)


def _format_discovered_title(title: str, platform: str) -> str:
    """Keep exact title casing for YouTube, title-case for others."""
    clean = re.sub(r"\s+", " ", (title or "").strip())
    if not clean:
        return ""
    if platform == "youtube":
        return clean
    return normalize_title(clean)


def _youtube_video_id(url: str) -> str | None:
    for pat in YOUTUBE_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    q = parse_qs(urlparse(url).query)
    vid = q.get("v", [None])[0]
    return vid


async def _fetch_youtube_metadata(client: httpx.AsyncClient, vid: str) -> dict:
    data = {}
    try:
        oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={vid}&format=json"
        oembed = await client.get(oembed_url)
        if oembed.status_code == 200:
            oj = oembed.json()
            title = (oj.get("title") or "").strip()
            if title:
                data["title"] = title
    except Exception:
        pass

    try:
        watch_url = f"https://www.youtube.com/watch?v={vid}"
        watch = await client.get(watch_url)
        if watch.status_code == 200 and watch.text:
            html = watch.text
            m = re.search(r'itemprop="datePublished"\s+content="([0-9]{4}-[0-9]{2}-[0-9]{2})"', html)
            if not m:
                m = re.search(r'"publishDate":"([0-9]{4}-[0-9]{2}-[0-9]{2})"', html)
            if m:
                parsed = parse_release_date(m.group(1))
                if parsed:
                    data["released_at"] = parsed
    except Exception:
        pass

    return data


async def _enrich_youtube_videos(videos: list[dict]):
    targets = [v for v in videos if (v.get("platform") or "").lower() == "youtube" and v.get("url")]
    if not targets:
        return

    cache: dict[str, dict] = {}
    sem = asyncio.Semaphore(4)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/126.0.0.0 Safari/537.36"
    }

    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=12.0) as client:
        async def enrich_one(v: dict):
            vid = _youtube_video_id(v.get("url", ""))
            if not vid:
                return
            if vid not in cache:
                async with sem:
                    cache[vid] = await _fetch_youtube_metadata(client, vid)
            meta = cache.get(vid) or {}
            title = (meta.get("title") or "").strip()
            if title:
                v["title"] = title
            if meta.get("released_at") and not v.get("released_at"):
                v["released_at"] = meta["released_at"]

        await asyncio.gather(*(enrich_one(v) for v in targets), return_exceptions=True)

# ── Parsing Logic ─────────────────────────────────────────────────────────────

def scrape_videos(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    found = []
    seen = set()

    base_info = detect_platform(base_url)
    if base_info and base_info[0] in {"youtube", "vimeo", "twitch", "dailymotion"}:
        base_title = extract_title(soup, fallback="")
        seen.add(normalize_url(base_url))
        found.append({
            "url":         base_info[1],
            "title":       normalize_title((base_title or "").strip()),
            "thumb":       base_info[2],
            "embed_url":   base_info[3],
            "platform":    base_info[0],
            "released_at": None,
            "cast_names":  None,
            "duration":    None,
        })

    def add(url, title=None, thumb=None, released_at=None,
            cast_names=None, duration=None):
        url = url.strip()
        if not url or url.startswith(("blob:", "data:", "javascript:")):
            return
        url = normalize_url(url)
        if not url or url in seen:
            return
        try:
            url = normalize_url(urljoin(base_url, url))
        except Exception:
            return
        if url in seen:
            return
        info = detect_platform(url)
        if not info:
            return
        platform, canonical, platform_thumb, embed = info
        if platform == "direct":
            base_host = urlparse(base_url).netloc.lower()
            target_host = urlparse(url).netloc.lower()
            if target_host != base_host:
                return
        seen.add(url)
        found.append({
            "url":         canonical,
            "title":       _format_discovered_title(title or "", platform),
            "thumb":       platform_thumb or thumb,
            "embed_url":   embed,
            "platform":    platform,
            "released_at": parse_release_date(released_at),
            "cast_names":  cast_names or None,
            "duration":    duration,
        })

    # 1. <video> / <source>
    for tag in soup.find_all(["video", "source"]):
        src = tag.get("src") or tag.get("data-src")
        if src:
            add(src, title=tag.get("title"))

    # 2. <iframe>
    for tag in soup.find_all("iframe"):
        src = tag.get("src") or tag.get("data-src")
        if src:
            add(src)

    # 3. <a href>
    LISTING_RE = re.compile(
        r'^(https?://[^/]+)?'
        r'/(videos?|scenes?|movies?|episodes?|clips?|content'
        r'|models?|pornstars?|categories|category|tags?|channels?'
        r'|studios?|sites?|networks?)/?$',
        re.I
    )
    CONTENT_RE = re.compile(
        r'/(video|scene|movie|episode|content|watch|clip|stream|embed'
        r'|v|play|porn|xxx|hd|full)s?/',
        re.I
    )

    def infer_release_from_tag(tag) -> str | None:
        candidates = [
            tag.get("datetime"),
            tag.get("data-date"),
            tag.get("data-time"),
            tag.get("data-created"),
            tag.get("data-published"),
            tag.get("title"),
        ]

        for sel in [
            "[class*='calendar']",
            "[class*='date']",
            "[class*='time']",
            "[class*='meta']",
            "time",
        ]:
            for el in tag.select(sel):
                if el is tag:
                    continue
                candidates.append(el.get("datetime"))
                candidates.append(el.get("title"))
                txt = el.get_text(" ", strip=True)
                if txt:
                    candidates.append(txt)

        parent = tag.parent
        for _ in range(2):
            if not parent:
                break
            for sel in ["[class*='calendar']", "[class*='date']", "[class*='time']", ".thumb__meta-item", "time"]:
                for el in parent.select(sel):
                    txt = el.get_text(" ", strip=True)
                    if txt:
                        candidates.append(txt)
            parent = parent.parent

        for c in candidates:
            parsed = parse_release_date(c)
            if parsed:
                return parsed
        return None

    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        if len(href) < 8:
            continue
        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        if LISTING_RE.match(href):
            continue
        if CONTENT_RE.search(href):
            title_text = (tag.get("title") or tag.get("aria-label") or
                          tag.get("data-title") or "").strip()
            if not title_text:
                for sel in [
                    "[class*='title']", "[class*='name']", "[class*='heading']",
                    "h3", "h4", "h5", "p", "span", "div",
                ]:
                    el = tag.select_one(sel)
                    if el:
                        t = el.get_text(strip=True)
                        if t and len(t) > 4 and re.search(r'[a-zA-Z]{3,}', t):
                            title_text = t
                            break
            if not title_text:
                title_text = tag.get_text(strip=True)

            title_text = re.sub(r'^\d{1,2}:\d{2}(:\d{2})?\s*', '', title_text)
            title_text = re.sub(r'\s*[\d.,]+[KkMm]?\s*(views?|watch\w*)?$', '', title_text)
            title_text = re.sub(r'\s+\d+$', '', title_text)
            title_text = title_text.strip()[:200]
            title_text = normalize_title(title_text)

            img = tag.select_one("img")
            thumb = None
            if img:
                thumb = (img.get("src") or img.get("data-src")
                         or img.get("data-lazy-src") or img.get("data-original")
                         or img.get("data-src-large"))
            release_hint = infer_release_from_tag(tag)
            add(href, title=title_text, thumb=thumb, released_at=release_hint)

    # 4. Deep-scan JSON
    for script_tag in soup.find_all("script", type="application/json"):
        raw = script_tag.string
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue

        def extract_thumb_from_images(v):
            PREF = ("poster", "card", "cover", "thumb", "large", "medium")
            if isinstance(v, dict):
                for pref in PREF:
                    for k2, v2 in v.items():
                        if pref in k2.lower():
                            if isinstance(v2, str) and v2.startswith("http"):
                                return v2
                            if isinstance(v2, dict):
                                url2 = v2.get("src") or v2.get("url") or v2.get("href")
                                if url2 and url2.startswith("http"):
                                    return url2
                for k2, v2 in v.items():
                    if isinstance(v2, str) and v2.startswith("http") and any(
                            ext in v2 for ext in (".jpg", ".jpeg", ".png", ".webp")):
                        return v2
                    if isinstance(v2, dict):
                        url2 = v2.get("src") or v2.get("url")
                        if url2 and isinstance(url2, str) and url2.startswith("http"):
                            return url2
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        url2 = item.get("src") or item.get("url") or item.get("href")
                        if url2 and isinstance(url2, str) and url2.startswith("http"):
                            return url2
            return None

        def extract_cast_from_obj(v):
            names = []
            if isinstance(v, list):
                for item in v:
                    if isinstance(item, str) and len(item) > 1:
                        names.append(item.strip())
                    elif isinstance(item, dict):
                        n = (item.get("name") or item.get("stageName") or
                             item.get("display_name") or item.get("displayName") or
                             item.get("fullName") or item.get("full_name") or
                             item.get("firstName", "") + " " + item.get("lastName", ""))
                        n = n.strip()
                        if n:
                            names.append(n)
            elif isinstance(v, dict):
                n = (v.get("name") or v.get("stageName") or v.get("displayName") or "").strip()
                if n:
                    names.append(n)
            return ", ".join(names) if names else None

        def deep_scan(obj):
            if isinstance(obj, dict):
                url_val = title_val = thumb_val = None
                id_val = slug_val = date_val = cast_val = dur_val = None
                children = []

                for k, v in obj.items():
                    kl = k.lower()
                    if isinstance(v, str):
                        _v_lower = v.lower()
                        if len(v) > 10 and any(kw in _v_lower for kw in (
                                "/video/", "/videos/", "/scene/", "/scenes/",
                                "/movie/", "/movies/", "/episode/", "/episodes/",
                                "/content/", "/watch/", "/clip/", "/clips/",
                                "/stream/", "/embed/", "/play/",
                                "/v/", "/porn/", "/hd/", "/xxx/")):
                            url_val = v
                        if kl in ("title", "name", "heading") and len(v) > 1:
                            title_val = v
                        if kl in ("slug", "permalink", "path", "url_slug") and len(v) > 3:
                            slug_val = v
                            if not title_val:
                                title_val = v.replace("-", " ").title()
                        if kl in ("thumb", "thumbnail", "poster", "cover",
                                  "image", "src", "imageurl", "screencap") and (v.startswith("http") or v.startswith("/")):
                            if not thumb_val:
                                thumb_val = v
                        if kl in ("datereleased", "releasedate", "date_released",
                                  "published_at", "publishedat", "airdate",
                                  "release_date", "dateadded", "date_added"):
                            date_val = v
                    elif isinstance(v, (int, float)):
                        if kl in ("datereleased", "releasedate", "published_at",
                                  "timestamp", "release_date"):
                            date_val = v
                        if kl in ("duration", "length", "runtime", "seconds"):
                            dur_val = int(v)
                    elif isinstance(v, dict):
                        if kl in ("images", "image", "thumbnails", "photos",
                                  "screenshots", "poster", "covers"):
                            t = extract_thumb_from_images(v)
                            if t and not thumb_val:
                                thumb_val = t
                        else:
                            children.append(v)
                    elif isinstance(v, list):
                        if kl in ("performers", "models", "cast", "stars",
                                  "talent", "actors", "actresses",
                                  "pornstars", "scenes_performers"):
                            c = extract_cast_from_obj(v)
                            if c:
                                cast_val = c
                        elif kl in ("images", "thumbnails", "screenshots", "photos"):
                            t = extract_thumb_from_images(v)
                            if t and not thumb_val:
                                thumb_val = t
                        else:
                            children.append(v)
                    if kl in ("videoid", "id", "sceneid") and v:
                        id_val = str(v)

                if url_val:
                    add(url_val, title=title_val, thumb=thumb_val,
                        released_at=date_val, cast_names=cast_val, duration=dur_val)
                elif slug_val and not slug_val.startswith("http"):
                    constructed = (f"/video/{id_val}/{slug_val}/" if id_val
                                   else f"/video/{slug_val}/")
                    add(constructed, title=title_val, thumb=thumb_val,
                        released_at=date_val, cast_names=cast_val, duration=dur_val)

                for child in children:
                    deep_scan(child)

            elif isinstance(obj, list):
                for item in obj:
                    deep_scan(item)

        deep_scan(data)

    # 5. Raw regex fallback
    for path in re.findall(
            r'/(?:[a-z]{2}/)?(?:videos?|scenes?|movies?|episodes?|content)'
            r'/[a-zA-Z0-9_-]{3,}/[a-zA-Z0-9_-]+',
            html):
        parts = path.rstrip("/").split("/")
        title = parts[-1].replace("-", " ").title() if len(parts) > 1 else "Video"
        add(path, title=title)

    if found:
        page_title = extract_title(soup)
        og = soup.select_one("meta[property='og:image']")
        og_thumb = og["content"] if og and og.get("content") else None
        for v in found:
            if not v["title"] and page_title:
                v["title"] = page_title
            if not v["thumb"] and og_thumb:
                v["thumb"] = og_thumb

    return found

# ── Scraper Crawlers ──────────────────────────────────────────────────────────

async def _fetch_with_curl(url: str, site_id: str | None = None) -> str | None:
    if not HAS_CURL_CFFI:
        return None
    try:
        cookies = {}
        if site_id:
            cp = cookie_path(site_id)
            if cp.exists():
                try:
                    saved = json.loads(cp.read_text())
                    cookies = {c["name"]: c["value"] for c in saved
                               if urlparse(url).netloc.endswith(
                                   c.get("domain", "").lstrip("."))}
                except Exception:
                    pass

        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        async with CurlSession(impersonate="chrome120") as session:
            r = await session.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                  "Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                              "image/avif,image/webp,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Referer": origin + "/",
                    "Origin": origin,
                },
                cookies=cookies,
                timeout=15,
                allow_redirects=True,
            )
            html = r.text
            if r.status_code == 404:
                log.warning(f"  curl_cffi: 404 for {url} — check URL is correct")
                return None
            if len(html) < 5000:
                log.info(f"  curl_cffi: response too small ({len(html)} chars, status={r.status_code}) — likely blocked")
                return None
            log.info(f"  curl_cffi: fetched {len(html):,} chars (status={r.status_code}): {url}")
            return html
    except Exception as e:
        log.warning(f"  curl_cffi failed for {url}: {e}")
        return None

async def _make_context(p, site_id: str) -> BrowserContext:
    browser = await p.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled",
              "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
    )
    context = await browser.new_context(
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
        viewport={"width": 1920, "height": 1080},
        ignore_https_errors=True,
        locale="en-US",
        timezone_id="America/New_York",
    )
    cp = cookie_path(site_id)
    if cp.exists():
        try:
            saved = json.loads(cp.read_text())
            await context.add_cookies(saved)
            log.info(f"  Restored {len(saved)} cookie(s) for site {site_id}")
        except Exception as e:
            log.warning(f"  Cookie restore failed: {e}")
    return browser, context

async def _save_cookies(context: BrowserContext, site_id: str):
    try:
        cookies = await context.cookies()
        cookie_path(site_id).write_text(json.dumps(cookies))
        log.info(f"  Saved {len(cookies)} cookie(s) for site {site_id}")
    except Exception as e:
        log.warning(f"  Cookie save failed: {e}")

AGE_GATE_SELECTORS = [
    "text=I am 18 or older", "text=Enter Site", "text=I agree",
    "button:has-text('Enter')", "#age-verify-submit", ".age-gate-btn",
    "text=Confirm Age", "[data-testid='age-gate-confirm']",
    "button:has-text('I am 18')",
]

async def _fetch_page(context: BrowserContext, url: str, first_page: bool = False) -> str:
    page = await context.new_page()
    await Stealth().apply_stealth_async(page)
    intercepted: list[str] = []

    async def on_response(response):
        ct = response.headers.get("content-type", "")
        if "json" in ct and response.status == 200:
            try:
                text = await response.text()
                if any(k in text for k in (
                                           '"videoId"', '"slug"', '"videos"',
                                           '"scene"', '"scenes"', '"movie"',
                                           '"movies"', '"title"', 'mp4', 'webm',
                                           '"dateReleased"', '"publishedAt"',
                                           '"release_date"', '"performers"',
                                           '"models"', '"episode"', '"content"',
                                           '"thumbnail"', '"poster"')):
                    intercepted.append(text)
            except Exception:
                pass

    page.on("response", lambda r: asyncio.ensure_future(on_response(r)))

    try:
        await page.goto(url, timeout=60000, wait_until="networkidle")
    except Exception:
        try:
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
        except Exception:
            pass

    if first_page:
        for sel in AGE_GATE_SELECTORS:
            try:
                btn = page.locator(sel)
                if await btn.is_visible(timeout=2000):
                    log.info(f"  Age gate clicked: '{sel}'")
                    await btn.click()
                    try:
                        await page.wait_for_load_state("networkidle", timeout=15000)
                    except Exception:
                        await page.wait_for_timeout(5000)
                    break
            except Exception:
                continue

    await page.wait_for_timeout(3000)

    try:
        await page.evaluate("""
            () => new Promise(resolve => {
                let y = 0;
                const step = () => {
                    window.scrollBy(0, 600);
                    y += 600;
                    if (y < document.body.scrollHeight) setTimeout(step, 250);
                    else resolve();
                };
                step();
            })
        """)
        await page.wait_for_timeout(1500)
    except Exception:
        pass

    html = await page.content()
    await page.close()

    log.info(f"  Fetched {len(html):,} chars + {len(intercepted)} API payload(s): {url}")
    for blob in intercepted:
        html += f'\n<script type="application/json">{blob}</script>'
    return html

# ── Sync Fallbacks ────────────────────────────────────────────────────────────

def _fetch_page_sync(context, url: str, first_page: bool = False) -> str:
    page = context.new_page()
    Stealth().apply_stealth_sync(page)
    try:
        try:
            page.goto(url, timeout=60000, wait_until="networkidle")
        except Exception:
            try:
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
            except Exception:
                pass

        if first_page:
            for sel in AGE_GATE_SELECTORS:
                try:
                    btn = page.locator(sel)
                    if btn.is_visible(timeout=2000):
                        log.info(f"  Age gate clicked: '{sel}'")
                        btn.click()
                        try:
                            page.wait_for_load_state("networkidle", timeout=15000)
                        except Exception:
                            page.wait_for_timeout(5000)
                        break
                except Exception:
                    continue

        page.wait_for_timeout(3000)
        try:
            page.evaluate("""
                () => new Promise(resolve => {
                    let y = 0;
                    const step = () => {
                        window.scrollBy(0, 600);
                        y += 600;
                        if (y < document.body.scrollHeight) setTimeout(step, 250);
                        else resolve();
                    };
                    step();
                })
            """)
            page.wait_for_timeout(1500)
        except Exception:
            pass

        return page.content()
    finally:
        page.close()

def _scan_site_sync(site: dict) -> list[dict]:
    base_url = site["url"]
    max_pages = _effective_max_pages(site)
    site_id = site["id"]
    all_videos = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled",
                  "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
            viewport={"width": 1920, "height": 1080},
            ignore_https_errors=True,
            locale="en-US",
            timezone_id="America/New_York",
        )

        cp = cookie_path(site_id)
        if cp.exists():
            try:
                saved = json.loads(cp.read_text())
                context.add_cookies(saved)
                log.info(f"  Restored {len(saved)} cookie(s) for site {site_id}")
            except Exception as e:
                log.warning(f"  Cookie restore failed: {e}")

        try:
            for page_num in range(1, max_pages + 1):
                url = page_url(base_url, page_num)
                log.info(f"  [sync] Page {page_num}/{max_pages}: {url}")
                try:
                    html = _fetch_page_sync(context, url, first_page=(page_num == 1))
                    videos = scrape_videos(html, url)
                    log.info(f"  [sync] Page {page_num}: {len(videos)} video(s)")
                    if not videos:
                        log.info(f"  [sync] Empty page {page_num}, stopping early")
                        break
                    all_videos.extend(videos)
                except Exception as e:
                    log.error(f"  [sync] Error on page {page_num}: {e}")
                    break

            try:
                cookies = context.cookies()
                cookie_path(site_id).write_text(json.dumps(cookies))
                log.info(f"  [sync] Saved {len(cookies)} cookie(s) for site {site_id}")
            except Exception as e:
                log.warning(f"  [sync] Cookie save failed: {e}")
        finally:
            browser.close()

    return all_videos

# ── Main Scraper Function ─────────────────────────────────────────────────────

async def scan_site(site: dict, push_func=None):
    """
    Crawls a site and writes newly discovered video metadata to the SQLite DB.
    Optionally accepts a `push_func` (async callable) to broadcast SSE updates.
    """
    async def push(msg: str):
        if push_func:
            await push_func(msg)

    base_url = site["url"]
    max_pages = _effective_max_pages(site)
    site_id = site["id"]
    log.info(f"Scanning {base_url} (max_pages={max_pages})")
    await push(f"SCAN_START|{site_id}|{site.get('name') or base_url}")

    all_videos: list[dict] = []

    try:
        async with async_playwright() as p:
            browser, context = await _make_context(p, site_id)
            try:
                for page_num in range(1, max_pages + 1):
                    url = page_url(base_url, page_num)
                    await push(f"PAGE|{site_id}|{page_num}|{max_pages}|{url}")
                    log.info(f"  Page {page_num}/{max_pages}: {url}")
                    try:
                        html = ""
                        fetch_error = None
                        for attempt in range(2):
                            try:
                                html = await _fetch_page(context, url, first_page=(page_num == 1))
                                break
                            except Exception as ex:
                                fetch_error = ex
                                if attempt == 0:
                                    await asyncio.sleep(1.0)
                        if fetch_error and not html:
                            raise fetch_error

                        if len(html) < 5000 and HAS_CURL_CFFI:
                            log.info(f"  Playwright got {len(html)} chars — trying curl_cffi fallback")
                            try:
                                curl_html = await asyncio.wait_for(
                                    _fetch_with_curl(url, site_id=site_id),
                                    timeout=15
                                )
                                if curl_html:
                                    html = curl_html
                            except asyncio.TimeoutError:
                                log.warning(f"  curl_cffi timed out for {url} — using Playwright result")

                        videos = scrape_videos(html, url)
                        log.info(f"  Page {page_num}: {len(videos)} video(s)")
                        await push(f"PAGE_DONE|{site_id}|{page_num}|{len(videos)}")
                        if not videos:
                            log.info(f"  Empty page {page_num}, stopping early")
                            break
                        all_videos.extend(videos)
                    except Exception as e:
                        log.error(f"  Error on page {page_num}: {e}")
                        await push(f"PAGE_ERROR|{site_id}|{page_num}|{e}")
                        break

                await _save_cookies(context, site_id)
            finally:
                await browser.close()
    except NotImplementedError as e:
        log.warning(f"Async Playwright not supported in this environment; using sync fallback: {e}")
        all_videos = await asyncio.to_thread(_scan_site_sync, site)
    except Exception as e:
        err = f"ERROR: {repr(e)}"
        log.error(f"Scan failed for {base_url}: {err}")
        with write_lock:
            with get_db() as db:
                db.execute(
                    "INSERT INTO scan_log (site_id, scanned_at, found, added, message) "
                    "VALUES (?,?,?,?,?)",
                    (site_id, now_iso(), 0, 0, err),
                )
                db.commit()
        await push(f"SCAN_ERROR|{site_id}|{err}")
        return

    # Deduplicate
    seen = set()
    unique = []
    for v in all_videos:
        if v["url"] not in seen:
            seen.add(v["url"])
            unique.append(v)

    unique = _apply_site_rules(site, unique)

    log.info(f"  {len(unique)} unique video(s) across {max_pages} page(s)")

    try:
        await _enrich_youtube_videos(unique)
    except Exception as e:
        log.warning(f"  YouTube metadata enrichment failed: {e}")

    def _upsert_videos_for_site(
        db_conn,
        target_site_id: str,
        videos: list[dict],
        mark_new_on_insert: bool,
    ) -> tuple[int, list[str]]:
        """Insert new videos and enrich existing records without duplicate-row failures."""
        inserted_count = 0
        inserted_ids: list[str] = []
        for v in videos:
            vid_id = short_id(f"{target_site_id}:{v['url']}")
            title = v["title"] or f"Scene {vid_id}"

            # Some sources rotate URL tokens for the same scene. If title+release match
            # an existing row for this site, refresh that row instead of creating a new one.
            released_at = v.get("released_at")
            if title and released_at:
                existing = db_conn.execute(
                    "SELECT id FROM videos "
                    "WHERE site_id=? AND LOWER(TRIM(title))=LOWER(TRIM(?)) "
                    "AND COALESCE(released_at,'')=COALESCE(?, '') "
                    "LIMIT 1",
                    (target_site_id, title, released_at),
                ).fetchone()
                if existing:
                    db_conn.execute(
                        "UPDATE videos SET "
                        "url = ?, "
                        "thumb = COALESCE(thumb, ?), "
                        "embed_url = COALESCE(embed_url, ?), "
                        "platform = COALESCE(platform, ?), "
                        "cast_names = COALESCE(cast_names, ?), "
                        "duration = COALESCE(duration, ?) "
                        "WHERE id = ?",
                        (
                            v["url"],
                            v.get("thumb"),
                            v.get("embed_url"),
                            v.get("platform"),
                            v.get("cast_names"),
                            v.get("duration"),
                            existing["id"],
                        ),
                    )
                    continue

            cur = db_conn.execute(
                "INSERT OR IGNORE INTO videos "
                "(id, site_id, title, url, thumb, embed_url, platform, "
                " found_at, released_at, cast_names, duration, is_new) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    vid_id,
                    target_site_id,
                    title,
                    v["url"],
                    v.get("thumb"),
                    v.get("embed_url"),
                    v.get("platform"),
                    now_iso(),
                    v.get("released_at"),
                    v.get("cast_names"),
                    v.get("duration"),
                    1 if mark_new_on_insert else 0,
                ),
            )
            if cur.rowcount:
                inserted_count += 1
                inserted_ids.append(vid_id)

            # Backfill sparse fields on already-known rows without overwriting data.
            db_conn.execute(
                "UPDATE videos SET "
                "title = CASE "
                "  WHEN ? = 'youtube' AND ? IS NOT NULL AND ? != '' "
                "       AND (title IS NULL OR title = '' OR LENGTH(?) > LENGTH(title)) "
                "    THEN ? "
                "  WHEN title IS NULL OR title = '' THEN COALESCE(?, title) "
                "  ELSE title END, "
                "thumb = COALESCE(thumb, ?), "
                "embed_url = COALESCE(embed_url, ?), "
                "platform = COALESCE(platform, ?), "
                "released_at = COALESCE(released_at, ?), "
                "cast_names = COALESCE(cast_names, ?), "
                "duration = COALESCE(duration, ?) "
                "WHERE site_id = ? AND url = ?",
                (
                    v.get("platform"),
                    title,
                    title,
                    title,
                    title,
                    title,
                    v.get("thumb"),
                    v.get("embed_url"),
                    v.get("platform"),
                    v.get("released_at"),
                    v.get("cast_names"),
                    v.get("duration"),
                    target_site_id,
                    v["url"],
                ),
            )
        return inserted_count, inserted_ids

    added = 0
    try:
        with write_lock:
            with get_db() as db:
                existing_before = db.execute(
                    "SELECT COUNT(*) FROM videos WHERE site_id=?",
                    (site_id,),
                ).fetchone()[0]
                baseline_import = existing_before == 0

                inserted_count, inserted_ids = _upsert_videos_for_site(
                    db,
                    site_id,
                    unique,
                    mark_new_on_insert=not baseline_import,
                )

                if inserted_ids:
                    placeholders = ",".join(["?"] * len(inserted_ids))
                    db.execute(
                        f"UPDATE videos SET is_new=0 WHERE site_id=? AND id NOT IN ({placeholders})",
                        [site_id, *inserted_ids],
                    )
                else:
                    db.execute("UPDATE videos SET is_new=0 WHERE site_id=?", (site_id,))

                added = 0 if baseline_import else inserted_count

                db.execute("UPDATE sites SET last_scan=? WHERE id=?", (now_iso(), site_id))
                msg = f"OK — {len(unique)} found across {max_pages} page(s), {added} new"
                db.execute(
                    "INSERT INTO scan_log (site_id, scanned_at, found, added, message) "
                    "VALUES (?,?,?,?,?)",
                    (site_id, now_iso(), len(unique), added, msg),
                )
                db.commit()
        _send_scan_notification(site, len(unique), added)
        await push(f"SCAN_DONE|{site_id}|{len(unique)}|{added}")
        log.info(f"  {msg}")

    except Exception as e:
        err = f"ERROR: {repr(e)}"
        log.error(f"  DB error: {err}")
        with write_lock:
            with get_db() as db:
                db.execute(
                    "INSERT INTO scan_log (site_id, scanned_at, found, added, message) "
                    "VALUES (?,?,?,?,?)",
                    (site_id, now_iso(), 0, 0, err),
                )
                db.commit()
        await push(f"SCAN_ERROR|{site_id}|{err}")

async def scan_all_sites(push_func=None):
    with get_db() as db:
        sites = [dict(r) for r in db.execute("SELECT * FROM sites")]
    for site in sites:
        await scan_site(site, push_func)
