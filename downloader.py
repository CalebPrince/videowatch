import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from yt_dlp import YoutubeDL

# Directory where downloaded videos are stored (same as VIDEOS_DIR in routes.py)
VIDEOS_DIR = Path(__file__).resolve().parent / "videos"
VIDEOS_DIR.mkdir(exist_ok=True)

log = logging.getLogger(__name__)

def _load_cookies_as_netscape(site_id: Optional[str]) -> Optional[Path]:
    """Convert the Playwright‑style JSON cookie file to Netscape format for yt‑dlp.
    Returns a temporary file path, or ``None`` if no cookies are available.
    """
    if not site_id:
        return None
    try:
        from scraper import cookie_path
    except Exception as e:
        log.warning(f"Could not import cookie_path: {e}")
        return None

    cp = cookie_path(site_id)
    if not cp.is_file():
        return None
    try:
        raw = json.loads(cp.read_text())
    except Exception as e:
        log.warning(f"Failed to read cookies for site {site_id}: {e}")
        return None

    lines = []
    for c in raw:
        domain = c.get("domain", "")
        if not domain.startswith('.'):
            domain = f".{domain}" if domain else ""
        flag = "TRUE" if c.get("hostOnly", False) else "FALSE"
        path = c.get("path", "/")
        secure = "TRUE" if c.get("secure", False) else "FALSE"
        expiration = str(int(c.get("expires", 0)))
        name = c.get("name", "")
        value = c.get("value", "")
        lines.append(f"{domain}\t{flag}\t{path}\t{secure}\t{expiration}\t{name}\t{value}")

    if not lines:
        return None

    tmp = Path(tempfile.NamedTemporaryFile(delete=False, suffix=".txt", dir=VIDEOS_DIR).name)
    tmp.write_text("\n".join(lines))
    return tmp

def download_video(url: str, site_id: Optional[str] = None) -> Optional[Path]:
    """Download a video using yt‑dlp with IDM‑style parallelism.
    Returns the absolute path to the saved file, or ``None`` on failure.
    """
    if not url:
        log.error("download_video called with empty URL")
        return None

    ydl_opts = {
        "outtmpl": str(VIDEOS_DIR / "%(title)s.%(ext)s"),
        "quiet": True,
        "nocheckcertificate": True,
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 8,
        "continuedl": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            )
        },
    }

    cookie_file = _load_cookies_as_netscape(site_id)
    if cookie_file:
        ydl_opts["cookiefile"] = str(cookie_file)

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = info.get("filepath") or info.get("_filename")
            if not filename:
                log.error("yt‑dlp did not return a filename for %s", url)
                return None
            saved_path = Path(filename)
            log.info("Video downloaded to %s", saved_path)
            return saved_path
    except Exception as e:
        log.error("yt‑dlp failed for %s: %s", url, e)
        return None
    finally:
        if cookie_file and cookie_file.exists():
            try:
                os.unlink(cookie_file)
            except Exception:
                pass
