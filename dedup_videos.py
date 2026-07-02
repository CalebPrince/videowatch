"""
One-shot script to merge duplicate videos that share the same site + numeric video ID.
Keeps the row with the most data; merges watched/favorite/archived states from all dupes.
Run once: python3 dedup_videos.py
"""
import re
import sqlite3
from pathlib import Path

DB_PATH = str(Path(__file__).resolve().parent / "videowatch.db")

def canonical_key(url: str) -> str:
    """Strip slug after numeric ID: /video/71709/some-slug → /video/71709"""
    return re.sub(r'^(https?://[^/]+/(?:video|scene|movie|episode|clip)s?/\d+)/.*$', r'\1', url.rstrip('/'))

def score(row: dict) -> int:
    """Higher score = more complete row. Prefer to keep this one."""
    s = 0
    if row['title']:          s += 3
    if row['thumb']:          s += 2
    if row['is_watched']:     s += 2
    if row['note']:           s += 2
    if row['is_favorite']:    s += 1
    if row['released_at']:    s += 1
    if row['cast_names']:     s += 1
    if row['duration']:       s += 1
    return s

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

rows = cur.execute("""
    SELECT id, site_id, url, title, thumb, is_watched, is_favorite, is_archived,
           note, released_at, cast_names, duration, found_at
    FROM videos
    ORDER BY found_at ASC
""").fetchall()

# Group by (site_id, canonical_key)
groups: dict[tuple, list] = {}
for row in rows:
    key = (row['site_id'], canonical_key(row['url']))
    groups.setdefault(key, []).append(dict(row))

dupes_found = 0
merged = 0

for key, group in groups.items():
    if len(group) < 2:
        continue
    dupes_found += len(group) - 1

    # Pick the best row to keep
    best = max(group, key=score)
    others = [r for r in group if r['id'] != best['id']]

    # Merge states from all dupes into best
    merged_watched   = best['is_watched']   or any(r['is_watched']   for r in others)
    merged_favorite  = best['is_favorite']  or any(r['is_favorite']  for r in others)
    merged_archived  = best['is_archived']  or any(r['is_archived']  for r in others)
    merged_note      = best['note'] or next((r['note'] for r in others if r['note']), None)
    merged_title     = best['title'] or next((r['title'] for r in others if r['title']), None)
    merged_thumb     = best['thumb'] or next((r['thumb'] for r in others if r['thumb']), None)
    merged_released  = best['released_at'] or next((r['released_at'] for r in others if r['released_at']), None)
    merged_cast      = best['cast_names'] or next((r['cast_names'] for r in others if r['cast_names']), None)
    merged_duration  = best['duration'] or next((r['duration'] for r in others if r['duration']), None)

    # Normalize the URL on the keeper to the canonical form
    canonical = canonical_key(best['url'])

    cur.execute("""
        UPDATE videos SET
            url          = ?,
            title        = ?,
            thumb        = ?,
            is_watched   = ?,
            is_favorite  = ?,
            is_archived  = ?,
            note         = ?,
            released_at  = ?,
            cast_names   = ?,
            duration     = ?
        WHERE id = ?
    """, (canonical, merged_title, merged_thumb,
          int(merged_watched), int(merged_favorite), int(merged_archived),
          merged_note, merged_released, merged_cast, merged_duration,
          best['id']))

    # Move collection memberships from dupes to keeper
    for other in others:
        cur.execute("""
            INSERT OR IGNORE INTO collection_videos (collection_id, video_id, added_at)
            SELECT collection_id, ?, added_at FROM collection_videos WHERE video_id = ?
        """, (best['id'], other['id']))

    # Delete dupes (cascade removes collection_videos rows)
    other_ids = [r['id'] for r in others]
    cur.execute(f"DELETE FROM videos WHERE id IN ({','.join('?'*len(other_ids))})", other_ids)
    merged += 1

conn.commit()
conn.close()

print(f"Done. Found {dupes_found} duplicate(s) across {merged} group(s). Merged and cleaned up.")
