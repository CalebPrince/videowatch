import sqlite3
import threading
import logging
from contextlib import contextmanager
from pathlib import Path

DB_PATH = str(Path(__file__).resolve().parent / "videowatch.db")
write_lock = threading.RLock()
log = logging.getLogger(__name__)

@contextmanager
def get_db():
    """Context manager for SQLite connections with 30s timeout."""
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    """Initialise schema and migrate tables if required."""
    def add_column_if_missing(db_conn, table: str, col: str, defval: str):
        try:
            db_conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defval}")
        except sqlite3.OperationalError as exc:
            # Expected when rerunning migrations against an already-updated schema.
            if "duplicate column name" in str(exc).lower():
                return
            log.exception("Schema migration failed for %s.%s", table, col)
            raise

    with write_lock:
        with get_db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sites (
                    id            TEXT PRIMARY KEY,
                    url           TEXT NOT NULL UNIQUE,
                    name          TEXT,
                    group_name    TEXT,
                    added_at      TEXT NOT NULL,
                    last_scan     TEXT,
                    max_pages     INTEGER DEFAULT 1,
                    scan_interval INTEGER DEFAULT 300,
                    rule_include_keywords TEXT DEFAULT '',
                    rule_exclude_keywords TEXT DEFAULT '',
                    rule_min_duration INTEGER DEFAULT 0,
                    scan_profile  TEXT DEFAULT 'balanced'
                );

                CREATE TABLE IF NOT EXISTS videos (
                    id            TEXT PRIMARY KEY,
                    site_id       TEXT NOT NULL,
                    title         TEXT,
                    url           TEXT NOT NULL,
                    thumb         TEXT,
                    embed_url     TEXT,
                    platform      TEXT,
                    found_at      TEXT NOT NULL,
                    released_at   TEXT,
                    cast_names    TEXT,
                    duration      INTEGER,
                    is_new        INTEGER DEFAULT 1,
                    is_favorite   INTEGER DEFAULT 0,
                    is_archived   INTEGER DEFAULT 0,
                    is_ignored    INTEGER DEFAULT 0,
                    duplicate_of  TEXT,
                    resolved_media_url TEXT,
                    resolved_kind TEXT,
                    resolved_at   TEXT,
                    download_status TEXT,
                    download_error TEXT,
                    local_file    TEXT,
                    FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE,
                    UNIQUE(site_id, url)
                );

                CREATE TABLE IF NOT EXISTS scan_log (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    site_id       TEXT,
                    scanned_at    TEXT NOT NULL,
                    found         INTEGER DEFAULT 0,
                    added         INTEGER DEFAULT 0,
                    message       TEXT
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                    key           TEXT PRIMARY KEY,
                    value         TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS users (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    username      TEXT NOT NULL UNIQUE,
                    password_salt TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role          TEXT NOT NULL DEFAULT 'viewer',
                    active        INTEGER NOT NULL DEFAULT 1,
                    created_at    TEXT NOT NULL,
                    updated_at    TEXT NOT NULL
                );
            """)
            # Ensure newer columns are present for schema evolution
            for col, defval in [
                ("max_pages",     "INTEGER DEFAULT 1"),
                ("scan_interval", "INTEGER DEFAULT 300"),
                ("rule_include_keywords", "TEXT DEFAULT ''"),
                ("rule_exclude_keywords", "TEXT DEFAULT ''"),
                ("rule_min_duration", "INTEGER DEFAULT 0"),
                ("scan_profile", "TEXT DEFAULT 'balanced'"),
                ("released_at",   "TEXT"),
                ("cast_names",    "TEXT"),
                ("duration",      "INTEGER"),
                ("is_favorite",   "INTEGER DEFAULT 0"),
                ("is_archived",   "INTEGER DEFAULT 0"),
                ("is_ignored",    "INTEGER DEFAULT 0"),
                ("duplicate_of",  "TEXT"),
                ("resolved_media_url", "TEXT"),
                ("resolved_kind", "TEXT"),
                ("resolved_at", "TEXT"),
                ("download_status", "TEXT"),
                ("download_error", "TEXT"),
                ("local_file", "TEXT"),
            ]:
                add_column_if_missing(db, "sites", col, defval)
                add_column_if_missing(db, "videos", col, defval)

            # Legacy schema used UNIQUE(url), which prevents the same video URL from
            # appearing under different monitored sites. Rebuild table to UNIQUE(site_id, url).
            idx_rows = db.execute("PRAGMA index_list(videos)").fetchall()
            has_site_url_unique = False
            for idx in idx_rows:
                if not idx[2]:
                    continue
                idx_name = idx[1]
                cols = [r[2] for r in db.execute(f"PRAGMA index_info({idx_name})").fetchall()]
                if cols == ["site_id", "url"]:
                    has_site_url_unique = True
                    break

            if not has_site_url_unique:
                log.info("Migrating videos table to UNIQUE(site_id, url)")
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS videos_new (
                        id            TEXT PRIMARY KEY,
                        site_id       TEXT NOT NULL,
                        title         TEXT,
                        url           TEXT NOT NULL,
                        thumb         TEXT,
                        embed_url     TEXT,
                        platform      TEXT,
                        found_at      TEXT NOT NULL,
                        released_at   TEXT,
                        cast_names    TEXT,
                        duration      INTEGER,
                        is_new        INTEGER DEFAULT 1,
                        is_favorite   INTEGER DEFAULT 0,
                        is_archived   INTEGER DEFAULT 0,
                        is_ignored    INTEGER DEFAULT 0,
                        duplicate_of  TEXT,
                        resolved_media_url TEXT,
                        resolved_kind TEXT,
                        resolved_at   TEXT,
                        download_status TEXT,
                        download_error TEXT,
                        local_file    TEXT,
                        FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE,
                        UNIQUE(site_id, url)
                    );

                    INSERT OR REPLACE INTO videos_new
                    (id, site_id, title, url, thumb, embed_url, platform, found_at, released_at, cast_names, duration, is_new, is_favorite, is_archived, is_ignored, duplicate_of,
                     resolved_media_url, resolved_kind, resolved_at, download_status, download_error, local_file)
                    SELECT id, site_id, title, url, thumb, embed_url, platform, found_at, released_at, cast_names, duration, COALESCE(is_new, 1),
                           COALESCE(is_favorite, 0), COALESCE(is_archived, 0), COALESCE(is_ignored, 0), duplicate_of,
                           resolved_media_url, resolved_kind, resolved_at, download_status, download_error, local_file
                    FROM videos;

                    DROP TABLE videos;
                    ALTER TABLE videos_new RENAME TO videos;
                """)

            add_column_if_missing(db, "sites", "group_name", "TEXT")
            add_column_if_missing(db, "sites", "notify_enabled", "INTEGER DEFAULT 1")
            add_column_if_missing(db, "sites", "owner", "TEXT")
            add_column_if_missing(db, "sites", "consecutive_failures", "INTEGER DEFAULT 0")
            add_column_if_missing(db, "sites", "alert_sent", "INTEGER DEFAULT 0")

            # Migrate sites table: replace UNIQUE(url) with UNIQUE(url, owner)
            idx_rows = db.execute("PRAGMA index_list(sites)").fetchall()
            has_url_owner_unique = any(
                [r[2] for r in db.execute(f"PRAGMA index_info({idx[1]})").fetchall()] == ["url", "owner"]
                for idx in idx_rows if idx[2]
            )
            if not has_url_owner_unique:
                log.info("Migrating sites table: UNIQUE(url) -> UNIQUE(url, owner)")
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS sites_new (
                        id            TEXT PRIMARY KEY,
                        url           TEXT NOT NULL,
                        name          TEXT,
                        group_name    TEXT,
                        added_at      TEXT NOT NULL,
                        last_scan     TEXT,
                        max_pages     INTEGER DEFAULT 1,
                        scan_interval INTEGER DEFAULT 300,
                        rule_include_keywords TEXT DEFAULT '',
                        rule_exclude_keywords TEXT DEFAULT '',
                        rule_min_duration INTEGER DEFAULT 0,
                        scan_profile  TEXT DEFAULT 'balanced',
                        notify_enabled INTEGER DEFAULT 1,
                        owner         TEXT,
                        UNIQUE(url, owner)
                    );
                    INSERT OR IGNORE INTO sites_new
                        SELECT id, url, name, group_name, added_at, last_scan, max_pages, scan_interval,
                               rule_include_keywords, rule_exclude_keywords, rule_min_duration,
                               scan_profile, notify_enabled, owner
                        FROM sites;
                    DROP TABLE sites;
                    ALTER TABLE sites_new RENAME TO sites;
                """)
                log.info("Sites table migration complete")
            add_column_if_missing(db, "videos", "is_watched", "INTEGER DEFAULT 0")
            add_column_if_missing(db, "videos", "last_watched_at", "TEXT")
            add_column_if_missing(db, "videos", "note", "TEXT")
            add_column_if_missing(db, "users", "email", "TEXT")
            add_column_if_missing(db, "users", "email_verified", "INTEGER DEFAULT 0")
            add_column_if_missing(db, "users", "onboarding_done", "INTEGER DEFAULT 0")
            add_column_if_missing(db, "users", "notify_new_videos", "INTEGER DEFAULT 0")
            add_column_if_missing(db, "users", "ui_theme", "TEXT DEFAULT 'light'")
            add_column_if_missing(db, "users", "plan", "TEXT DEFAULT 'free'")
            add_column_if_missing(db, "users", "tos_accepted_at", "TEXT")
            db.execute("""
                CREATE TABLE IF NOT EXISTS video_tags (
                    video_id TEXT NOT NULL,
                    tag      TEXT NOT NULL,
                    owner    TEXT NOT NULL,
                    PRIMARY KEY (video_id, tag, owner)
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_video_tags_owner ON video_tags(owner)")
            db.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp   TEXT NOT NULL,
                    username    TEXT NOT NULL,
                    action      TEXT NOT NULL,
                    detail      TEXT,
                    ip          TEXT
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log(timestamp DESC)")
            db.execute("""
                CREATE TABLE IF NOT EXISTS email_verifications (
                    token       TEXT PRIMARY KEY,
                    username    TEXT NOT NULL,
                    expires_at  TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS password_resets (
                    token       TEXT PRIMARY KEY,
                    username    TEXT NOT NULL,
                    expires_at  TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS waitlist (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    email      TEXT NOT NULL UNIQUE,
                    source     TEXT DEFAULT 'mobile',
                    created_at TEXT NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_waitlist_created ON waitlist(created_at DESC)")
            db.execute("""
                CREATE TABLE IF NOT EXISTS api_tokens (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner       TEXT NOT NULL,
                    token_hash  TEXT NOT NULL UNIQUE,
                    label       TEXT,
                    created_at  TEXT NOT NULL,
                    last_used_at TEXT
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_api_tokens_owner ON api_tokens(owner)")
            db.execute("""
                CREATE TABLE IF NOT EXISTS collections (
                    id         TEXT PRIMARY KEY,
                    owner      TEXT NOT NULL,
                    name       TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_collections_owner ON collections(owner)")
            db.execute("""
                CREATE TABLE IF NOT EXISTS collection_videos (
                    collection_id TEXT NOT NULL,
                    video_id      TEXT NOT NULL,
                    added_at      TEXT NOT NULL,
                    PRIMARY KEY (collection_id, video_id),
                    FOREIGN KEY (collection_id) REFERENCES collections(id) ON DELETE CASCADE,
                    FOREIGN KEY (video_id)      REFERENCES videos(id)      ON DELETE CASCADE
                )
            """)
            db.execute(
                "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
                ("autoscan_enabled", "0"),
            )

            # FTS5 full-text search index on video title and cast_names
            try:
                db.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS videos_fts USING fts5(
                        title,
                        cast_names,
                        content='videos',
                        content_rowid='rowid',
                        tokenize='unicode61'
                    )
                """)
                db.execute("""
                    CREATE TRIGGER IF NOT EXISTS videos_fts_insert
                    AFTER INSERT ON videos BEGIN
                        INSERT INTO videos_fts(rowid, title, cast_names)
                        VALUES (new.rowid, COALESCE(new.title,''), COALESCE(new.cast_names,''));
                    END
                """)
                db.execute("""
                    CREATE TRIGGER IF NOT EXISTS videos_fts_delete
                    AFTER DELETE ON videos BEGIN
                        INSERT INTO videos_fts(videos_fts, rowid, title, cast_names)
                        VALUES ('delete', old.rowid, COALESCE(old.title,''), COALESCE(old.cast_names,''));
                    END
                """)
                db.execute("""
                    CREATE TRIGGER IF NOT EXISTS videos_fts_update
                    AFTER UPDATE ON videos BEGIN
                        INSERT INTO videos_fts(videos_fts, rowid, title, cast_names)
                        VALUES ('delete', old.rowid, COALESCE(old.title,''), COALESCE(old.cast_names,''));
                        INSERT INTO videos_fts(rowid, title, cast_names)
                        VALUES (new.rowid, COALESCE(new.title,''), COALESCE(new.cast_names,''));
                    END
                """)
                # Use FTS rebuild command to safely populate from existing videos
                db.execute("INSERT INTO videos_fts(videos_fts) VALUES('rebuild')")
            except Exception as e:
                log.warning(f"FTS5 init failed (non-fatal): {e}")

            db.commit()
