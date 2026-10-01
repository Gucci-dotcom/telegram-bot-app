import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# draft -> scheduled -> publishing -> published | failed
STATUSES = ("draft", "scheduled", "publishing", "published", "failed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    caption      TEXT NOT NULL DEFAULT '',
    media        TEXT NOT NULL DEFAULT '[]',   -- JSON: [{"file": "abc.jpg", "type": "photo"|"video"}]
    status       TEXT NOT NULL,
    scheduled_at TEXT,                         -- ISO UTC
    published_at TEXT,
    message_ids  TEXT NOT NULL DEFAULT '[]',
    error        TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_status_time ON posts (status, scheduled_at);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Database:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        post = dict(row)
        post["media"] = json.loads(post["media"])
        post["message_ids"] = json.loads(post["message_ids"])
        return post

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def create_post(
        self,
        caption: str,
        media: list[dict],
        status: str,
        scheduled_at: datetime | None = None,
    ) -> dict:
        assert status in STATUSES
        cur = self._execute(
            "INSERT INTO posts (caption, media, status, scheduled_at, created_at) VALUES (?, ?, ?, ?, ?)",
            (caption, json.dumps(media), status, to_iso(scheduled_at), to_iso(utcnow())),
        )
        return self.get_post(cur.lastrowid)

    def get_post(self, post_id: int) -> dict | None:
        return self._row(self._execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone())

    def update_post(self, post_id: int, **fields: Any) -> dict | None:
        if not fields:
            return self.get_post(post_id)
        values = []
        for key, value in fields.items():
            if key not in {"caption", "media", "status", "scheduled_at", "published_at", "message_ids", "error"}:
                raise KeyError(key)
            if key in ("media", "message_ids"):
                value = json.dumps(value)
            elif isinstance(value, datetime):
                value = to_iso(value)
            values.append(value)
        assignments = ", ".join(f"{key} = ?" for key in fields)
        self._execute(f"UPDATE posts SET {assignments} WHERE id = ?", (*values, post_id))
        return self.get_post(post_id)

    def claim_for_publishing(self, post_id: int, from_statuses: tuple[str, ...]) -> bool:
        """Атомарно переводит пост в 'publishing', чтобы он не ушёл в канал дважды."""
        marks = ",".join("?" * len(from_statuses))
        cur = self._execute(
            f"UPDATE posts SET status = 'publishing' WHERE id = ? AND status IN ({marks})",
            (post_id, *from_statuses),
        )
        return cur.rowcount == 1

    def delete_post(self, post_id: int) -> None:
        self._execute("DELETE FROM posts WHERE id = ?", (post_id,))

    def list_posts(self, status: str | None = None, limit: int = 30, offset: int = 0) -> list[dict]:
        if status == "scheduled":
            # очередь: ближайшие сверху
            sql = "SELECT * FROM posts WHERE status = 'scheduled' ORDER BY scheduled_at ASC, id ASC LIMIT ? OFFSET ?"
            params: tuple = (limit, offset)
        else:
            where, params = ("WHERE status = ?", (status,)) if status else ("", ())
            sql = (
                f"SELECT * FROM posts {where} "
                "ORDER BY COALESCE(published_at, scheduled_at, created_at) DESC, id DESC LIMIT ? OFFSET ?"
            )
            params = (*params, limit, offset)
        return [self._row(r) for r in self._execute(sql, params).fetchall()]

    def due_posts(self, now: datetime) -> list[dict]:
        rows = self._execute(
            "SELECT * FROM posts WHERE status = 'scheduled' AND scheduled_at <= ? ORDER BY scheduled_at, id",
            (to_iso(now),),
        ).fetchall()
        return [self._row(r) for r in rows]

    def scheduled_times(self) -> list[datetime]:
        rows = self._execute("SELECT scheduled_at FROM posts WHERE status = 'scheduled'").fetchall()
        return [from_iso(r["scheduled_at"]) for r in rows if r["scheduled_at"]]

    def stats(self) -> dict[str, int]:
        counts = {s: 0 for s in STATUSES}
        for row in self._execute("SELECT status, COUNT(*) AS n FROM posts GROUP BY status").fetchall():
            counts[row["status"]] = row["n"]
        return counts
