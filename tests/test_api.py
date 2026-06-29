import os
import sqlite3
import pytest
import base64
import secrets
from pathlib import Path
from datetime import datetime, timezone
from httpx2 import AsyncClient
from httpx2 import ASGITransport

from server import app, DB_PATH
import scraper
import routes
from db import get_db, write_lock


class _FakeResolverHttpResponse:
    def __init__(self, url: str, text: str = "", content_type: str = "text/html", status_code: int = 200):
        self.url = url
        self.text = text
        self.status_code = status_code
        self.headers = {"content-type": content_type}


class _FakeResolverHttpClient:
    def __init__(self, response: _FakeResolverHttpResponse):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, headers=None):
        return self.response


@pytest.fixture
async def client():
    # Keep auth state deterministic across tests by resetting DB-stored credentials.
    admin_user = os.environ.get("VIDEOWATCH_AUTH_USERNAME", "admin")
    admin_pass = os.environ.get("VIDEOWATCH_AUTH_PASSWORD", "admin123")

    with write_lock:
        with get_db() as db:
            db.execute(
                "DELETE FROM app_settings WHERE key IN ('auth_username','auth_password_salt','auth_password_hash')"
            )
            db.execute("DELETE FROM users")
            db.commit()

    salt = secrets.token_bytes(16)
    salt_b64 = base64.b64encode(salt).decode("ascii")
    hash_b64 = routes._pbkdf2_hash(admin_pass, salt_b64)
    with write_lock:
        with get_db() as db:
            now = scraper.now_iso()
            db.execute(
                "INSERT INTO users (username, password_salt, password_hash, role, active, created_at, updated_at) "
                "VALUES (?,?,?,?,1,?,?)",
                (admin_user, salt_b64, hash_b64, "admin", now, now),
            )
            db.commit()

    async with AsyncClient(transport=ASGITransport(app), base_url="http://testserver") as client:
        login_resp = await client.post(
            "/api/auth/login",
            json={
                "username": os.environ.get("VIDEOWATCH_AUTH_USERNAME", "admin"),
                "password": os.environ.get("VIDEOWATCH_AUTH_PASSWORD", "admin123"),
            },
        )
        assert login_resp.status_code == 200
        yield client


@pytest.mark.anyio
async def test_root_serves_frontend(client):
    response = await client.get("/")
    assert response.status_code == 200
    assert "html" in response.headers["content-type"]


@pytest.mark.anyio
async def test_health_endpoint(client):
    response = await client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] in ("ok", "error")
    assert data["db_path"] == str(DB_PATH)


def test_database_connection():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sites'")
    assert cur.fetchone() is not None
    conn.close()


@pytest.mark.anyio
async def test_sites_endpoint(client):
    response = await client.get("/api/sites")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    for site in data:
        assert "id" in site
        assert "url" in site
        assert "total_count" in site


@pytest.mark.anyio
async def test_scan_flow_prevents_duplicate_inserts_and_backfills(monkeypatch):
    site_url = "https://example.com/videos/"
    site_id = scraper.short_id(site_url)
    first_payload = [{
        "url": "https://example.com/video/demo-scene/",
        "title": "Demo Scene",
        "thumb": None,
        "embed_url": None,
        "platform": "direct",
        "released_at": None,
        "cast_names": None,
        "duration": None,
    }]
    second_payload = [{
        "url": "https://example.com/video/demo-scene/",
        "title": "Demo Scene",
        "thumb": "https://example.com/thumb.jpg",
        "embed_url": None,
        "platform": "direct",
        "released_at": "2026-01-01T00:00:00",
        "cast_names": "Performer A",
        "duration": 120,
    }]

    with write_lock:
        with get_db() as db:
            db.execute("DELETE FROM videos WHERE site_id=?", (site_id,))
            db.execute("DELETE FROM sites WHERE id=?", (site_id,))
            db.execute(
                "INSERT INTO sites (id, url, name, group_name, added_at, max_pages, scan_interval) "
                "VALUES (?,?,?,?,?,?,?)",
                (site_id, site_url, "", "", scraper.now_iso(), 1, 300),
            )
            db.commit()

    def _raise_not_implemented():
        raise NotImplementedError("test fallback")

    monkeypatch.setattr(scraper, "async_playwright", _raise_not_implemented)
    monkeypatch.setattr(scraper, "_scan_site_sync", lambda _site: first_payload)
    await scraper.scan_site({"id": site_id, "url": site_url, "max_pages": 1})

    monkeypatch.setattr(scraper, "_scan_site_sync", lambda _site: second_payload)
    await scraper.scan_site({"id": site_id, "url": site_url, "max_pages": 1})

    with get_db() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM videos WHERE site_id=? AND url=?",
            (site_id, first_payload[0]["url"]),
        ).fetchone()[0]
        row = db.execute(
            "SELECT cast_names, duration, thumb FROM videos WHERE site_id=? AND url=?",
            (site_id, first_payload[0]["url"]),
        ).fetchone()

    assert count == 1
    assert row["cast_names"] == "Performer A"
    assert row["duration"] == 120
    assert row["thumb"] == "https://example.com/thumb.jpg"

    with write_lock:
        with get_db() as db:
            db.execute("DELETE FROM videos WHERE site_id=?", (site_id,))
            db.execute("DELETE FROM sites WHERE id=?", (site_id,))
            db.commit()


@pytest.mark.anyio
async def test_rescan_keeps_old_rows_seen_when_no_new_videos(monkeypatch):
    site_url = "https://example.com/videos/"
    site_id = scraper.short_id(site_url)
    payload = [{
        "url": "https://example.com/video/demo-scene/",
        "title": "Demo Scene",
        "thumb": None,
        "embed_url": None,
        "platform": "direct",
        "released_at": "2026-01-01T00:00:00",
        "cast_names": None,
        "duration": None,
    }]

    with write_lock:
        with get_db() as db:
            db.execute("DELETE FROM videos WHERE site_id=?", (site_id,))
            db.execute("DELETE FROM sites WHERE id=?", (site_id,))
            db.execute(
                "INSERT INTO sites (id, url, name, group_name, added_at, max_pages, scan_interval) "
                "VALUES (?,?,?,?,?,?,?)",
                (site_id, site_url, "", "", scraper.now_iso(), 1, 300),
            )
            db.commit()

    def _raise_not_implemented():
        raise NotImplementedError("test fallback")

    monkeypatch.setattr(scraper, "async_playwright", _raise_not_implemented)
    monkeypatch.setattr(scraper, "_scan_site_sync", lambda _site: payload)
    await scraper.scan_site({"id": site_id, "url": site_url, "max_pages": 1})

    with write_lock:
        with get_db() as db:
            db.execute("UPDATE videos SET is_new=0 WHERE site_id=?", (site_id,))
            db.commit()

    monkeypatch.setattr(scraper, "_scan_site_sync", lambda _site: payload)
    await scraper.scan_site({"id": site_id, "url": site_url, "max_pages": 1})

    with get_db() as db:
        row = db.execute(
            "SELECT is_new FROM videos WHERE site_id=? AND url=?",
            (site_id, payload[0]["url"]),
        ).fetchone()
        added = db.execute(
            "SELECT added FROM scan_log WHERE site_id=? ORDER BY id DESC LIMIT 1",
            (site_id,),
        ).fetchone()[0]

    assert row is not None
    assert row["is_new"] == 0
    assert added == 0

    with write_lock:
        with get_db() as db:
            db.execute("DELETE FROM videos WHERE site_id=?", (site_id,))
            db.execute("DELETE FROM sites WHERE id=?", (site_id,))
            db.commit()


@pytest.mark.anyio
async def test_first_scan_baseline_import_is_not_marked_new(monkeypatch):
    site_url = "https://example.com/videos/"
    site_id = scraper.short_id(site_url)
    payload = [{
        "url": "https://example.com/video/baseline-one/",
        "title": "Baseline One",
        "thumb": None,
        "embed_url": None,
        "platform": "direct",
        "released_at": "2025-01-01T00:00:00",
        "cast_names": None,
        "duration": None,
    }]

    with write_lock:
        with get_db() as db:
            db.execute("DELETE FROM videos WHERE site_id=?", (site_id,))
            db.execute("DELETE FROM sites WHERE id=?", (site_id,))
            db.execute(
                "INSERT INTO sites (id, url, name, group_name, added_at, max_pages, scan_interval) "
                "VALUES (?,?,?,?,?,?,?)",
                (site_id, site_url, "", "", scraper.now_iso(), 1, 300),
            )
            db.commit()

    def _raise_not_implemented():
        raise NotImplementedError("test fallback")

    monkeypatch.setattr(scraper, "async_playwright", _raise_not_implemented)
    monkeypatch.setattr(scraper, "_scan_site_sync", lambda _site: payload)
    await scraper.scan_site({"id": site_id, "url": site_url, "max_pages": 1})

    with get_db() as db:
        row = db.execute(
            "SELECT is_new FROM videos WHERE site_id=? AND url=?",
            (site_id, payload[0]["url"]),
        ).fetchone()
        added = db.execute(
            "SELECT added FROM scan_log WHERE site_id=? ORDER BY id DESC LIMIT 1",
            (site_id,),
        ).fetchone()[0]

    assert row is not None
    assert row["is_new"] == 0
    assert added == 0

    with write_lock:
        with get_db() as db:
            db.execute("DELETE FROM videos WHERE site_id=?", (site_id,))
            db.execute("DELETE FROM sites WHERE id=?", (site_id,))
            db.commit()


@pytest.mark.anyio
async def test_video_download_persists_local_file_metadata(client, monkeypatch):
    site_url = "https://example.com/videos/"
    site_id = scraper.short_id(site_url)
    video_id = "video-download-demo"
    downloaded = routes.VIDEOS_DIR / f"{video_id}.mp4"

    with write_lock:
        with get_db() as db:
            db.execute("DELETE FROM videos WHERE id=?", (video_id,))
            db.execute("DELETE FROM sites WHERE id=?", (site_id,))
            db.execute(
                "INSERT INTO sites (id, url, name, group_name, added_at, max_pages, scan_interval) VALUES (?,?,?,?,?,?,?)",
                (site_id, site_url, "Example", "", scraper.now_iso(), 1, 300),
            )
            db.execute(
                "INSERT INTO videos (id, site_id, title, url, found_at, platform, is_new) VALUES (?,?,?,?,?,?,?)",
                (video_id, site_id, "Download Demo", "https://example.com/video/demo.mp4", scraper.now_iso(), "direct", 0),
            )
            db.commit()

    async def fake_resolve(_url: str, _expected_title: str | None = None):
        return {
            "resolved_url": "https://cdn.example.com/demo.mp4",
            "kind": "direct",
            "reason": "test",
            "headers": {"Referer": "https://example.com/"},
        }

    async def fake_download(_video_id: str, _resolved_url: str, _headers: dict[str, str]):
        downloaded.write_bytes(b"demo-bytes")
        return {"filename": downloaded.name, "content_type": "video/mp4"}

    monkeypatch.setattr(routes, "_resolve_video_source_impl", fake_resolve)
    monkeypatch.setattr(routes, "_download_media_file", fake_download)

    response = await client.post(f"/api/videos/{video_id}/download")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "downloaded"
    assert data["local_url"] == f"/api/video/{video_id}"

    with get_db() as db:
        row = db.execute(
            "SELECT local_file, download_status, resolved_media_url, resolved_kind, download_error FROM videos WHERE id=?",
            (video_id,),
        ).fetchone()

    assert row["local_file"] == downloaded.name
    assert row["download_status"] == "downloaded"
    assert row["resolved_media_url"] == "https://cdn.example.com/demo.mp4"
    assert row["resolved_kind"] == "direct"
    assert row["download_error"] is None
    assert downloaded.exists()

    downloaded.unlink(missing_ok=True)
    with write_lock:
        with get_db() as db:
            db.execute("DELETE FROM videos WHERE id=?", (video_id,))
            db.execute("DELETE FROM sites WHERE id=?", (site_id,))
            db.commit()


@pytest.mark.anyio
async def test_video_resolve_falls_back_to_browser_capture(client, monkeypatch):
    site_url = "https://example.com/videos/"
    site_id = scraper.short_id(site_url)

    with write_lock:
        with get_db() as db:
            db.execute("DELETE FROM sites WHERE id=?", (site_id,))
            db.execute(
                "INSERT INTO sites (id, url, name, group_name, added_at, max_pages, scan_interval) VALUES (?,?,?,?,?,?,?)",
                (site_id, site_url, "Example", "", scraper.now_iso(), 1, 300),
            )
            db.commit()

    fake_response = _FakeResolverHttpResponse(
        url="https://example.com/watch/demo",
        text="<html><body>No direct media here</body></html>",
        content_type="text/html",
        status_code=200,
    )
    monkeypatch.setattr(routes.httpx, "AsyncClient", lambda *args, **kwargs: _FakeResolverHttpClient(fake_response))

    async def fake_browser_resolver(_url: str, diagnostics: dict | None = None, expected_title: str | None = None):
        return {
            "resolved_url": "https://cdn.example.com/videos/demo.mp4",
            "kind": "direct",
            "reason": "resolved from browser network",
            "headers": {"Referer": "https://example.com/"},
            "diagnostics": diagnostics or {},
        }

    monkeypatch.setattr(routes, "_resolve_video_source_with_browser", fake_browser_resolver)

    response = await client.get("/api/video-resolve", params={"url": "https://example.com/watch/demo"})
    assert response.status_code == 200
    data = response.json()
    assert data["resolved_url"] == "https://cdn.example.com/videos/demo.mp4"
    assert data["kind"] == "direct"
    assert data["reason"] == "resolved from browser network"

    with write_lock:
        with get_db() as db:
            db.execute("DELETE FROM sites WHERE id=?", (site_id,))
            db.commit()


@pytest.mark.anyio
async def test_video_resolve_debug_includes_failure_diagnostics(client, monkeypatch):
    site_url = "https://example.com/videos/"
    site_id = scraper.short_id(site_url)

    with write_lock:
        with get_db() as db:
            db.execute("DELETE FROM sites WHERE id=?", (site_id,))
            db.execute(
                "INSERT INTO sites (id, url, name, group_name, added_at, max_pages, scan_interval) VALUES (?,?,?,?,?,?,?)",
                (site_id, site_url, "Example", "", scraper.now_iso(), 1, 300),
            )
            db.commit()

    fake_response = _FakeResolverHttpResponse(
        url="https://example.com/watch/demo",
        text="<html><body>No direct media here</body></html>",
        content_type="text/html",
        status_code=200,
    )
    monkeypatch.setattr(routes.httpx, "AsyncClient", lambda *args, **kwargs: _FakeResolverHttpClient(fake_response))

    async def fake_browser_resolver(_url: str, diagnostics: dict | None = None, expected_title: str | None = None):
        diagnostics = diagnostics or {}
        diagnostics["browser_used"] = True
        diagnostics["browser_network_candidates"] = 0
        diagnostics["browser_json_candidates"] = 0
        diagnostics["browser_html_candidates"] = 0
        diagnostics["browser_video_tag_src"] = False
        return {
            "resolved_url": None,
            "kind": "none",
            "reason": "no playable media discovered",
            "headers": {"Referer": "https://example.com/"},
            "diagnostics": diagnostics,
        }

    monkeypatch.setattr(routes, "_resolve_video_source_with_browser", fake_browser_resolver)

    response = await client.get("/api/video-resolve", params={"url": "https://example.com/watch/demo", "debug": "true"})
    assert response.status_code == 200
    data = response.json()
    assert data["resolved_url"] is None
    assert data["reason"] == "no playable media discovered"
    assert data["diagnostics"]["final_url"] == "https://example.com/watch/demo"
    assert data["diagnostics"]["http_status"] == 200
    assert data["diagnostics"]["html_candidates"] == 0
    assert data["diagnostics"]["browser_used"] is True
    assert "html-candidates=0" in data["diagnostics_summary"]

    with write_lock:
        with get_db() as db:
            db.execute("DELETE FROM sites WHERE id=?", (site_id,))
            db.commit()


@pytest.mark.anyio
async def test_video_download_falls_back_to_embed_url_after_404(client, monkeypatch):
    site_url = "https://example.com/videos/"
    site_id = scraper.short_id(site_url)
    video_id = "video-download-embed-fallback"
    downloaded = routes.VIDEOS_DIR / f"{video_id}.mp4"

    with write_lock:
        with get_db() as db:
            db.execute("DELETE FROM videos WHERE id=?", (video_id,))
            db.execute("DELETE FROM sites WHERE id=?", (site_id,))
            db.execute(
                "INSERT INTO sites (id, url, name, group_name, added_at, max_pages, scan_interval) VALUES (?,?,?,?,?,?,?)",
                (site_id, site_url, "Example", "", scraper.now_iso(), 1, 300),
            )
            db.execute(
                "INSERT INTO videos (id, site_id, title, url, embed_url, found_at, platform, is_new) VALUES (?,?,?,?,?,?,?,?)",
                (
                    video_id,
                    site_id,
                    "Fallback Demo",
                    "https://example.com/watch/missing",
                    "https://example.com/embed/demo-player",
                    scraper.now_iso(),
                    "direct",
                    0,
                ),
            )
            db.commit()

    seen_urls = []
    failing_page_urls = {
        "https://example.com/watch/missing",
        "https://example.com/watch/missing/",
    }

    async def fake_resolve(url: str, _expected_title: str | None = None):
        seen_urls.append(url)
        if url in failing_page_urls:
            return {
                "resolved_url": None,
                "kind": "none",
                "reason": "no playable media discovered",
                "headers": {"Referer": "https://example.com/"},
                "diagnostics": {
                    "http_status": 404,
                    "final_url": url,
                    "http_content_type": "text/html",
                    "html_candidates": 0,
                },
            }
        return {
            "resolved_url": "https://cdn.example.com/demo.mp4",
            "kind": "direct",
            "reason": "resolved from page metadata",
            "headers": {"Referer": "https://example.com/"},
            "diagnostics": {
                "http_status": 200,
                "final_url": url,
                "http_content_type": "text/html",
                "html_candidates": 1,
            },
        }

    async def fake_download(_video_id: str, _resolved_url: str, _headers: dict[str, str]):
        downloaded.write_bytes(b"demo-bytes")
        return {"filename": downloaded.name, "content_type": "video/mp4"}

    monkeypatch.setattr(routes, "_resolve_video_source_impl", fake_resolve)
    monkeypatch.setattr(routes, "_download_media_file", fake_download)

    response = await client.post(f"/api/videos/{video_id}/download")
    assert response.status_code == 200
    assert response.json()["resolved_url"] == "https://cdn.example.com/demo.mp4"
    assert seen_urls[:2] == [
        "https://example.com/watch/missing",
        "https://example.com/watch/missing/",
    ]
    assert "https://example.com/embed/demo-player" in seen_urls

    with get_db() as db:
        row = db.execute(
            "SELECT local_file, download_status, resolved_media_url FROM videos WHERE id=?",
            (video_id,),
        ).fetchone()

    assert row["local_file"] == downloaded.name
    assert row["download_status"] == "downloaded"
    assert row["resolved_media_url"] == "https://cdn.example.com/demo.mp4"

    downloaded.unlink(missing_ok=True)
    with write_lock:
        with get_db() as db:
            db.execute("DELETE FROM videos WHERE id=?", (video_id,))
            db.execute("DELETE FROM sites WHERE id=?", (site_id,))
            db.commit()


@pytest.mark.anyio
async def test_rescan_url_churn_same_title_release_does_not_create_new(monkeypatch):
    site_url = "https://example.com/videos/"
    site_id = scraper.short_id(site_url)
    first = [{
        "url": "https://example.com/video/demo-scene/?token=abc",
        "title": "Demo Scene",
        "thumb": None,
        "embed_url": None,
        "platform": "direct",
        "released_at": "2026-01-01T00:00:00",
        "cast_names": None,
        "duration": 120,
    }]
    second = [{
        "url": "https://example.com/video/demo-scene/?token=xyz",
        "title": "Demo Scene",
        "thumb": "https://example.com/thumb.jpg",
        "embed_url": None,
        "platform": "direct",
        "released_at": "2026-01-01T00:00:00",
        "cast_names": "Performer A",
        "duration": 120,
    }]

    with write_lock:
        with get_db() as db:
            db.execute("DELETE FROM videos WHERE site_id=?", (site_id,))
            db.execute("DELETE FROM sites WHERE id=?", (site_id,))
            db.execute(
                "INSERT INTO sites (id, url, name, group_name, added_at, max_pages, scan_interval) "
                "VALUES (?,?,?,?,?,?,?)",
                (site_id, site_url, "", "", scraper.now_iso(), 1, 300),
            )
            db.commit()

    def _raise_not_implemented():
        raise NotImplementedError("test fallback")

    monkeypatch.setattr(scraper, "async_playwright", _raise_not_implemented)
    monkeypatch.setattr(scraper, "_scan_site_sync", lambda _site: first)
    await scraper.scan_site({"id": site_id, "url": site_url, "max_pages": 1})

    monkeypatch.setattr(scraper, "_scan_site_sync", lambda _site: second)
    await scraper.scan_site({"id": site_id, "url": site_url, "max_pages": 1})

    with get_db() as db:
        count = db.execute("SELECT COUNT(*) FROM videos WHERE site_id=?", (site_id,)).fetchone()[0]
        latest = db.execute(
            "SELECT url, cast_names, is_new FROM videos WHERE site_id=? ORDER BY found_at DESC LIMIT 1",
            (site_id,),
        ).fetchone()
        added = db.execute(
            "SELECT added FROM scan_log WHERE site_id=? ORDER BY id DESC LIMIT 1",
            (site_id,),
        ).fetchone()[0]

    assert count == 1
    assert latest is not None
    assert latest["url"] == second[0]["url"]
    assert latest["cast_names"] == "Performer A"
    assert latest["is_new"] == 0
    assert added == 0

    with write_lock:
        with get_db() as db:
            db.execute("DELETE FROM videos WHERE site_id=?", (site_id,))
            db.execute("DELETE FROM sites WHERE id=?", (site_id,))
            db.commit()


def test_parse_release_date_supports_relative_time():
    rel = scraper.parse_release_date("6 days ago")
    assert rel is not None
    dt = datetime.fromisoformat(rel).replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
    assert 5 <= age_days <= 7


def test_scrape_videos_extracts_relative_release_from_card_meta():
    html = """
    <div class=\"thumb\">
        <a href=\"/video/demo-scene/\" class=\"thumb__link\" title=\"Demo Scene\">
            <img src=\"https://example.com/thumb.jpg\" />
        </a>
        <span class=\"thumb__meta-item\">6 days ago</span>
    </div>
    """
    videos = scraper.scrape_videos(html, "https://example.com/videos/")
    assert len(videos) == 1
    assert videos[0]["released_at"] is not None


def test_scrape_videos_preserves_youtube_title_casing():
    assert scraper._format_discovered_title("NASA LIVE STREAM", "youtube") == "NASA LIVE STREAM"


def test_parse_duration_iso8601():
    assert scraper.parse_duration("PT1H2M3S") == 3723
    assert scraper.parse_duration("PT45M") == 2700
    assert scraper.parse_duration("P1DT2H") == 93600


def test_parse_duration_clock_and_numeric():
    assert scraper.parse_duration("12:34") == 754
    assert scraper.parse_duration("1:02:03") == 3723
    assert scraper.parse_duration(600) == 600
    assert scraper.parse_duration("600") == 600


def test_parse_duration_rejects_garbage():
    assert scraper.parse_duration("") is None
    assert scraper.parse_duration(None) is None
    assert scraper.parse_duration("not-a-duration") is None
    assert scraper.parse_duration(0) is None


def test_normalize_url_encodes_query_values():
    out = scraper.normalize_url("https://example.com/videos/?q=hot stuff&a=b")
    assert " " not in out
    assert "q=hot+stuff" in out
    # tracking params are still stripped
    assert "utm_source" not in scraper.normalize_url(
        "https://example.com/v/?utm_source=x&id=7"
    )


def test_scrape_videos_extracts_ldjson_videoobject():
    html = """
    <html><head>
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "VideoObject",
      "name": "JSON-LD Sample Scene",
      "thumbnailUrl": "https://example.com/ld-thumb.jpg",
      "uploadDate": "2021-05-04",
      "duration": "PT22M30S",
      "actor": [{"@type": "Person", "name": "Jane Doe"}],
      "url": "https://example.com/video/ld-sample-scene/"
    }
    </script>
    </head><body></body></html>
    """
    videos = scraper.scrape_videos(html, "https://example.com/videos/")
    match = [v for v in videos if "ld-sample-scene" in v["url"]]
    assert len(match) == 1
    v = match[0]
    assert v["title"] == "Json Ld Sample Scene"
    assert v["thumb"] == "https://example.com/ld-thumb.jpg"
    assert v["released_at"] == "2021-05-04T00:00:00"
    assert v["duration"] == 1350
    assert v["cast_names"] == "Jane Doe"


def test_scrape_videos_extracts_ldjson_from_graph():
    html = """
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@graph": [
        {"@type": "WebPage", "name": "ignore me"},
        {"@type": "VideoObject", "name": "Graph Scene",
         "url": "https://example.com/video/graph-scene/",
         "duration": "PT5M"}
      ]
    }
    </script>
    """
    videos = scraper.scrape_videos(html, "https://example.com/videos/")
    match = [v for v in videos if "graph-scene" in v["url"]]
    assert len(match) == 1
    assert match[0]["duration"] == 300


def test_migrate_legacy_videos_dir_moves_old_downloads(tmp_path):
    legacy_dir = tmp_path / "legacy_videos"
    target_dir = tmp_path / "project_videos"
    legacy_dir.mkdir()
    target_dir.mkdir()

    old_file = legacy_dir / "video-one.mp4"
    old_file.write_bytes(b"legacy-bytes")
    existing_target = target_dir / "video-two.mp4"
    existing_target.write_bytes(b"keep-me")
    duplicate_legacy = legacy_dir / "video-two.mp4"
    duplicate_legacy.write_bytes(b"duplicate")

    result = routes._migrate_legacy_videos_dir(target_dir=target_dir, legacy_dir=legacy_dir)

    assert result == {"moved": 1, "skipped": 1, "errors": 0}
    assert not old_file.exists()
    assert (target_dir / "video-one.mp4").read_bytes() == b"legacy-bytes"
    assert duplicate_legacy.exists()
    assert existing_target.read_bytes() == b"keep-me"


def test_choose_best_media_candidate_prefers_primary_video_over_ad_asset():
    candidates = [
        ("https://cdn.example.com/ads/preroll-trailer.mp4", "direct", "resolved from browser network"),
        ("https://example.com/media/demo-scene-main.mp4", "direct", "resolved from browser DOM"),
        ("https://cdn.example.com/previews/demo-scene-preview.mp4", "direct", "resolved from browser network"),
    ]

    chosen = routes._choose_best_media_candidate(
        candidates,
        "https://example.com/watch/demo-scene",
        "https://example.com/watch/demo-scene",
    )

    assert chosen is not None
    assert chosen[0] == "https://example.com/media/demo-scene-main.mp4"
    assert chosen[2] == "resolved from browser DOM"


def test_choose_best_media_candidate_uses_title_to_avoid_same_host_ad():
    candidates = [
        ("https://example.com/media/player-roll-ad-clip.mp4", "direct", "resolved from browser network"),
        ("https://example.com/media/night-moves-scene-1080p.mp4", "direct", "resolved from browser network"),
    ]

    chosen = routes._choose_best_media_candidate(
        candidates,
        "https://example.com/watch/night-moves-scene",
        "https://example.com/watch/night-moves-scene",
        "Night Moves Scene",
    )

    assert chosen is not None
    assert chosen[0] == "https://example.com/media/night-moves-scene-1080p.mp4"


@pytest.mark.anyio
async def test_fresh_scan_endpoint_clears_existing_videos(monkeypatch, client):
    site_url = "https://example.com/videos/"
    site_id = scraper.short_id(site_url)
    video_url = "https://example.com/video/already-stored/"

    with write_lock:
        with get_db() as db:
            db.execute("DELETE FROM videos WHERE site_id=?", (site_id,))
            db.execute("DELETE FROM sites WHERE id=?", (site_id,))
            db.execute(
                "INSERT INTO sites (id, url, name, group_name, added_at, max_pages, scan_interval) "
                "VALUES (?,?,?,?,?,?,?)",
                (site_id, site_url, "", "", scraper.now_iso(), 1, 300),
            )
            db.execute(
                "INSERT INTO videos (id, site_id, title, url, thumb, embed_url, platform, found_at, is_new) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (scraper.short_id(video_url), site_id, "Stored", video_url, None, None, "direct", scraper.now_iso(), 1),
            )
            db.commit()

    monkeypatch.setattr(routes, "_run_scan_all_sync", lambda: None)
    resp = await client.post("/api/scan/fresh")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["deleted"] >= 1

    with get_db() as db:
        remaining = db.execute("SELECT COUNT(*) FROM videos WHERE site_id=?", (site_id,)).fetchone()[0]
    assert remaining == 0

    with write_lock:
        with get_db() as db:
            db.execute("DELETE FROM videos WHERE site_id=?", (site_id,))
            db.execute("DELETE FROM sites WHERE id=?", (site_id,))
            db.commit()


@pytest.mark.anyio
async def test_scan_automation_toggle(client):
    get_before = await client.get("/api/scan/automation")
    assert get_before.status_code == 200
    assert "enabled" in get_before.json()

    paused = await client.post("/api/scan/automation/toggle", json={"enabled": False})
    assert paused.status_code == 200
    assert paused.json()["enabled"] is False

    get_paused = await client.get("/api/scan/automation")
    assert get_paused.status_code == 200
    assert get_paused.json()["enabled"] is False

    resumed = await client.post("/api/scan/automation/toggle", json={"enabled": True})
    assert resumed.status_code == 200
    assert resumed.json()["enabled"] is True


@pytest.mark.anyio
async def test_change_password_flow_hashes_and_applies_credentials(client):
    tracked_keys = {"auth_username", "auth_password_salt", "auth_password_hash"}
    original = {}
    with get_db() as db:
        rows = db.execute(
            "SELECT key, value FROM app_settings WHERE key IN ('auth_username','auth_password_salt','auth_password_hash')"
        ).fetchall()
        for row in rows:
            original[row["key"]] = row["value"]

    try:
        changed = await client.post(
            "/api/auth/change-password",
            json={
                "current_password": os.environ.get("VIDEOWATCH_AUTH_PASSWORD", "admin123"),
                "new_password": "newpass123",
                "confirm_password": "newpass123",
            },
        )
        assert changed.status_code == 200
        assert changed.json()["ok"] is True

        await client.post("/api/auth/logout")

        old_login = await client.post(
            "/api/auth/login",
            json={
                "username": os.environ.get("VIDEOWATCH_AUTH_USERNAME", "admin"),
                "password": os.environ.get("VIDEOWATCH_AUTH_PASSWORD", "admin123"),
            },
        )
        assert old_login.status_code == 401

        new_login = await client.post(
            "/api/auth/login",
            json={"username": os.environ.get("VIDEOWATCH_AUTH_USERNAME", "admin"), "password": "newpass123"},
        )
        assert new_login.status_code == 200
        assert new_login.json()["authenticated"] is True

        with get_db() as db:
            user_row = db.execute(
                "SELECT password_hash, password_salt FROM users WHERE username=?",
                (os.environ.get("VIDEOWATCH_AUTH_USERNAME", "admin"),),
            ).fetchone()
        assert user_row is not None
        assert user_row["password_hash"]
        assert user_row["password_salt"]
    finally:
        with write_lock:
            with get_db() as db:
                for key in tracked_keys:
                    if key in original:
                        db.execute(
                            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, original[key]),
                        )
                    else:
                        db.execute("DELETE FROM app_settings WHERE key=?", (key,))
                db.commit()


@pytest.mark.anyio
async def test_admin_can_add_user_and_update_role(client):
    create = await client.post(
        "/api/users",
        json={"username": "viewer1", "password": "viewerpass1", "role": "viewer"},
    )
    assert create.status_code == 200
    assert create.json()["ok"] is True

    listed = await client.get("/api/users")
    assert listed.status_code == 200
    users = listed.json()
    assert any(u["username"] == "viewer1" and u["role"] == "viewer" for u in users)

    patched = await client.patch("/api/users/viewer1", json={"role": "admin", "active": True})
    assert patched.status_code == 200
    assert patched.json()["role"] == "admin"

    with write_lock:
        with get_db() as db:
            db.execute("DELETE FROM users WHERE username='viewer1'")
            db.commit()
