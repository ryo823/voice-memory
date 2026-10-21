"""SQLite storage for voice memories.

The DB path comes from VOICE_MEMORY_DB, else <project root>/data/voice_memory.db,
where the project root is resolved from this file's location.
"""
from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

MAX_TITLE = 200
MAX_SUMMARY = 2_000
MAX_TRANSCRIPT = 100_000
MAX_TAGS = 20
MAX_TAG_LEN = 50
MAX_QUERY = 200
MAX_LIMIT = 50
DEFAULT_LIMIT = 10
SNIPPET_LEN = 200

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id          TEXT PRIMARY KEY,
    recorded_at TEXT NOT NULL,
    title       TEXT NOT NULL,
    summary     TEXT NOT NULL DEFAULT '',
    transcript  TEXT NOT NULL,
    audio_path  TEXT,
    source      TEXT NOT NULL DEFAULT 'manual',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_recorded_at ON memories(recorded_at DESC);
CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS memory_tags (
    memory_id TEXT    NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    tag_id    INTEGER NOT NULL REFERENCES tags(id)     ON DELETE CASCADE,
    PRIMARY KEY (memory_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_memory_tags_tag ON memory_tags(tag_id);
"""


class ValidationError(ValueError):
    """Invalid user input."""


class NotFoundError(LookupError):
    """Requested record does not exist."""


def default_db_path() -> Path:
    env = os.environ.get("VOICE_MEMORY_DB")
    if env:
        return Path(env).expanduser().resolve()
    return PROJECT_ROOT / "data" / "voice_memory.db"


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    """Create tables if missing. Safe to call repeatedly."""
    with closing(connect(db_path)) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean(value: str | None, field: str, max_len: int, required: bool) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    value = value.strip()
    if "\x00" in value:
        raise ValidationError(f"{field} contains a NUL character")
    if required and not value:
        raise ValidationError(f"{field} is required")
    if len(value) > max_len:
        raise ValidationError(f"{field} must be at most {max_len} characters")
    return value


def _clean_tags(tags: list[str] | None) -> list[str]:
    tags = tags or []
    if not isinstance(tags, list):
        raise ValidationError("tags must be a list of strings")
    if len(tags) > MAX_TAGS:
        raise ValidationError(f"at most {MAX_TAGS} tags are allowed")
    out: list[str] = []
    for t in tags:
        t = _clean(t, "tag", MAX_TAG_LEN, required=True)
        if t not in out:
            out.append(t)
    return out


def _clean_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValidationError("limit must be a positive integer")
    return min(limit, MAX_LIMIT)


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _snippet(text: str, keyword: str | None = None) -> str:
    if not keyword:
        return text[:SNIPPET_LEN] + ("…" if len(text) > SNIPPET_LEN else "")
    idx = text.lower().find(keyword.lower())
    if idx < 0:
        return _snippet(text)
    start = max(0, idx - SNIPPET_LEN // 4)
    end = min(len(text), start + SNIPPET_LEN)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def _tags_for(conn: sqlite3.Connection, memory_ids: list[str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {i: [] for i in memory_ids}
    if not memory_ids:
        return result
    marks = ",".join("?" * len(memory_ids))
    rows = conn.execute(
        f"SELECT mt.memory_id, t.name FROM memory_tags mt JOIN tags t ON t.id = mt.tag_id "
        f"WHERE mt.memory_id IN ({marks}) ORDER BY t.name",
        memory_ids,
    )
    for r in rows:
        result[r["memory_id"]].append(r["name"])
    return result


def _summarize(conn, rows, keyword=None) -> list[dict]:
    tags = _tags_for(conn, [r["id"] for r in rows])
    return [
        {
            "id": r["id"],
            "recorded_at": r["recorded_at"],
            "title": r["title"],
            "summary": r["summary"],
            "excerpt": _snippet(r["transcript"], keyword),
            "tags": tags[r["id"]],
        }
        for r in rows
    ]


def save_memory(
    title: str,
    transcript: str,
    summary: str = "",
    tags: list[str] | None = None,
    source: str = "manual",
    recorded_at: str | None = None,
    audio_path: str | None = None,
    db_path: Path | str | None = None,
) -> dict:
    title = _clean(title, "title", MAX_TITLE, True)
    transcript = _clean(transcript, "transcript", MAX_TRANSCRIPT, True)
    summary = _clean(summary, "summary", MAX_SUMMARY, False)
    source = _clean(source, "source", 50, True)
    tag_list = _clean_tags(tags)
    if recorded_at:
        try:
            recorded_at = datetime.fromisoformat(recorded_at).isoformat(timespec="seconds")
        except ValueError:
            raise ValidationError("recorded_at must be an ISO 8601 datetime") from None
    else:
        recorded_at = _now()
    if audio_path:
        p = Path(audio_path)
        if p.is_absolute() or ".." in p.parts:
            raise ValidationError("audio_path must be a relative path without '..'")
        audio_path = str(p)
    else:
        audio_path = None

    memory_id = str(uuid.uuid4())
    with closing(connect(db_path)) as conn:
        with conn:  # one transaction
            conn.execute(
                "INSERT INTO memories (id, recorded_at, title, summary, transcript, audio_path, source, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (memory_id, recorded_at, title, summary, transcript, audio_path, source, _now()),
            )
            for name in tag_list:
                conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
                conn.execute(
                    "INSERT INTO memory_tags (memory_id, tag_id) "
                    "SELECT ?, id FROM tags WHERE name = ?",
                    (memory_id, name),
                )
    return {"id": memory_id, "recorded_at": recorded_at, "title": title, "tags": tag_list}


def search_memories(query: str, limit: int = DEFAULT_LIMIT, db_path=None) -> list[dict]:
    query = _clean(query, "query", MAX_QUERY, True)
    limit = _clean_limit(limit)
    pat = f"%{_like_escape(query)}%"
    sql = """
        SELECT m.* FROM memories m
        WHERE m.title LIKE ?1 ESCAPE '\\' OR m.summary LIKE ?1 ESCAPE '\\'
           OR m.transcript LIKE ?1 ESCAPE '\\'
           OR EXISTS (SELECT 1 FROM memory_tags mt JOIN tags t ON t.id = mt.tag_id
                      WHERE mt.memory_id = m.id AND t.name LIKE ?1 ESCAPE '\\')
        ORDER BY m.recorded_at DESC LIMIT ?2
    """
    with closing(connect(db_path)) as conn:
        rows = conn.execute(sql, (pat, limit)).fetchall()
        return _summarize(conn, rows, query)


def get_memory(memory_id: str, db_path=None) -> dict:
    memory_id = _clean(memory_id, "id", 64, True)
    with closing(connect(db_path)) as conn:
        row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"memory not found: {memory_id}")
        d = dict(row)
        d["tags"] = _tags_for(conn, [memory_id])[memory_id]
        return d


def recent_memories(limit: int = DEFAULT_LIMIT, db_path=None) -> list[dict]:
    limit = _clean_limit(limit)
    with closing(connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM memories ORDER BY recorded_at DESC, created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return _summarize(conn, rows)


def list_tags(db_path=None) -> list[dict]:
    with closing(connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT t.name, COUNT(mt.memory_id) AS count FROM tags t "
            "LEFT JOIN memory_tags mt ON mt.tag_id = t.id GROUP BY t.id "
            "ORDER BY count DESC, t.name"
        ).fetchall()
        return [dict(r) for r in rows]


def init_cli() -> None:
    init_db()
    print(f"initialized: {default_db_path()}")
