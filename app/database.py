import sqlite3
import json
import os
from typing import List, Dict, Optional

DB_PATH = "/app/data/rabbithole.db"


def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS items (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id      TEXT,
                title         TEXT,
                url           TEXT NOT NULL,
                channel       TEXT,
                subject_area  TEXT,
                file_path     TEXT,
                docmost_page_id TEXT,
                processed_at  TEXT,
                source        TEXT DEFAULT 'manual',
                status        TEXT DEFAULT 'queued',
                status_message TEXT,
                error_message TEXT,
                summary       TEXT,
                tags          TEXT,
                created_at    TEXT DEFAULT (datetime('now'))
            )
        """)
        # Migrate: add status_message if missing
        try:
            conn.execute("ALTER TABLE items ADD COLUMN status_message TEXT")
            conn.commit()
        except Exception:
            pass
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_url ON items(url)")
        conn.commit()


def add_item(url: str, source: str = "manual", subject_area_override: str = None) -> int:
    """Insert item. Returns rowid, or -1 if URL already exists."""
    with get_conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO items (url, source, status, subject_area) VALUES (?, ?, 'queued', ?)",
                (url, source, subject_area_override)
            )
            conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            row = conn.execute("SELECT id FROM items WHERE url = ?", (url,)).fetchone()
            return row["id"] if row else -1


def update_item(item_id: int, **kwargs):
    if not kwargs:
        return
    fields = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [item_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE items SET {fields} WHERE id = ?", values)
        conn.commit()


def get_queued_items() -> List[Dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM items WHERE status = 'queued' ORDER BY created_at ASC LIMIT 5"
        ).fetchall()
        return [dict(r) for r in rows]


def get_items(limit: int = 20, offset: int = 0,
              subject_area: str = None, search: str = None,
              include_active: bool = False) -> List[Dict]:
    if include_active:
        q = "SELECT * FROM items WHERE 1=1"
    else:
        q = "SELECT * FROM items WHERE status NOT IN ('queued', 'processing')"
    p = []
    if subject_area:
        q += " AND subject_area = ?"; p.append(subject_area)
    if search:
        q += " AND (title LIKE ? OR summary LIKE ? OR tags LIKE ? OR channel LIKE ?)"
        s = f"%{search}%"; p.extend([s, s, s, s])
    q += " ORDER BY COALESCE(processed_at, created_at) DESC LIMIT ? OFFSET ?"
    p.extend([limit, offset])
    with get_conn() as conn:
        rows = conn.execute(q, p).fetchall()
        result = []
        for r in rows:
            item = dict(r)
            if item.get("tags") and isinstance(item["tags"], str):
                try:    item["tags"] = json.loads(item["tags"])
                except: item["tags"] = []
            result.append(item)
        return result


def get_item(item_id: int) -> Optional[Dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        if item.get("tags") and isinstance(item["tags"], str):
            try:    item["tags"] = json.loads(item["tags"])
            except: item["tags"] = []
        return item


def get_stats() -> Dict:
    with get_conn() as conn:
        total   = conn.execute("SELECT COUNT(*) FROM items WHERE status='done'").fetchone()[0]
        queued  = conn.execute("SELECT COUNT(*) FROM items WHERE status='queued'").fetchone()[0]
        proc    = conn.execute("SELECT COUNT(*) FROM items WHERE status='processing'").fetchone()[0]
        errors  = conn.execute("SELECT COUNT(*) FROM items WHERE status='error'").fetchone()[0]
        by_area = conn.execute(
            "SELECT subject_area, COUNT(*) as count FROM items "
            "WHERE status='done' GROUP BY subject_area ORDER BY count DESC"
        ).fetchall()
        return {"total": total, "queued": queued, "processing": proc,
                "errors": errors, "by_subject_area": [dict(r) for r in by_area]}


def delete_item(item_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        conn.commit()
