"""SQLite storage for voice memories.

The DB path comes from VOICE_MEMORY_DB, else <project root>/data/voice_memory.db,
where the project root is resolved from this file's location.
"""
from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
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
DEFAULT_PENDING_DAYS = 7
MAX_PENDING_DAYS = 365
REGISTRATION_KINDS = ("event", "task", "none")

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
-- A memory with a row here has been reviewed for calendar/TODO suggestions and is not proposed again.
-- kind 'none' means "reviewed, nothing registered" (external_id stays NULL).
CREATE TABLE IF NOT EXISTS registrations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id   TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('event', 'task', 'none')),
    external_id TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_registrations_memory ON registrations(memory_id);
-- A memory with a row here was already mailed as a candidate (proposal-only run), so the daily run skips it.
-- Separate from registrations: the memory stays "pending" for the interactive /suggest-actions review.
CREATE TABLE IF NOT EXISTS suggestions (
    memory_id    TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    suggested_at TEXT NOT NULL
);
-- Keyed by memory_id, not memories.rowid: memories has a TEXT primary key, so VACUUM may renumber rowids.
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    memory_id UNINDEXED, title, summary, transcript, tags, tokenize = 'trigram'
);
"""

# trigram FTS cannot match terms shorter than this many characters; those use LIKE.
FTS_MIN_TERM = 3


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
        ensure_fts(conn)


def ensure_fts(conn: sqlite3.Connection) -> None:
    """Index records of an existing DB that gained the FTS table (also heals count drift).

    Only the row COUNT is compared. It catches a missing/empty index and rows added or removed behind the
    index's back, but not same-count drift (a memory edited or re-tagged outside save_memory, or one stale
    row swapped for another); `rebuild_fts()` fixes those on demand. Callers must have no open transaction.
    """
    if _fts_count(conn) != _memories_count(conn):
        rebuild_fts(conn)


def _memories_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]


def _fts_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0]


def rebuild_fts(conn: sqlite3.Connection) -> int:
    """Rebuild the whole FTS index from memories + tags. Idempotent and all-or-nothing.

    Returns the number of indexed memories. Never touches the source tables. Refuses to run inside the
    caller's open transaction (committing it here would silently publish the caller's half-done work).
    """
    if conn.in_transaction:
        raise RuntimeError("rebuild_fts() must not be called inside an open transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM memories_fts")
        conn.execute(
            "INSERT INTO memories_fts (memory_id, title, summary, transcript, tags) "
            "SELECT m.id, m.title, m.summary, m.transcript, COALESCE((SELECT group_concat(t.name, ' ') "
            "FROM memory_tags mt JOIN tags t ON t.id = mt.tag_id WHERE mt.memory_id = m.id), '') "
            "FROM memories m"
        )
        n = _fts_count(conn)
        conn.commit()
        return n
    except BaseException:
        conn.rollback()
        raise


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


def _snippet(text: str, keyword: str | list[str] | None = None) -> str:
    if isinstance(keyword, list):
        keyword = next((k for k in keyword if k.lower() in text.lower()), None)
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
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Save a memory. With `conn`, the insert joins the caller's transaction (no commit here)."""
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

    def insert(c: sqlite3.Connection) -> None:
        c.execute(
            "INSERT INTO memories (id, recorded_at, title, summary, transcript, audio_path, source, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (memory_id, recorded_at, title, summary, transcript, audio_path, source, _now()),
        )
        for name in tag_list:
            c.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
            c.execute(
                "INSERT INTO memory_tags (memory_id, tag_id) "
                "SELECT ?, id FROM tags WHERE name = ?",
                (memory_id, name),
            )
        # Same transaction as the rows above; this is the only write path, so keep them in step.
        c.execute(
            "INSERT INTO memories_fts (memory_id, title, summary, transcript, tags) VALUES (?,?,?,?,?)",
            (memory_id, title, summary, transcript, " ".join(tag_list)),
        )

    if conn is not None:
        insert(conn)
    else:
        with closing(connect(db_path)) as own:
            with own:  # one transaction
                insert(own)
    return {"id": memory_id, "recorded_at": recorded_at, "title": title, "tags": tag_list}


def _fts_term(term: str) -> str:
    """Quote one term as an FTS5 string so operators/wildcards in it are literal."""
    return '"' + term.replace('"', '""') + '"'


def search_memories(query: str, limit: int = DEFAULT_LIMIT, db_path=None) -> list[dict]:
    """AND search over whitespace-separated terms in title, summary, transcript and tags.

    Terms of FTS_MIN_TERM+ characters use the trigram FTS index; shorter ones fall back
    to a literal LIKE. Newest first.
    """
    query = _clean(query, "query", MAX_QUERY, True)
    limit = _clean_limit(limit)
    terms = list(dict.fromkeys(query.split()))
    long_terms = [t for t in terms if len(t) >= FTS_MIN_TERM]
    short_terms = [t for t in terms if len(t) < FTS_MIN_TERM]

    where: list[str] = []
    params: list = []
    if long_terms:
        where.append("m.id IN (SELECT memory_id FROM memories_fts WHERE memories_fts MATCH ?)")
        params.append(" AND ".join(_fts_term(t) for t in long_terms))
    for t in short_terms:
        where.append(
            "(m.title LIKE ? ESCAPE '\\' OR m.summary LIKE ? ESCAPE '\\' OR m.transcript LIKE ? ESCAPE '\\' "
            "OR EXISTS (SELECT 1 FROM memory_tags mt JOIN tags t ON t.id = mt.tag_id "
            "WHERE mt.memory_id = m.id AND t.name LIKE ? ESCAPE '\\'))"
        )
        params += [f"%{_like_escape(t)}%"] * 4
    sql = f"SELECT m.* FROM memories m WHERE {' AND '.join(where)} ORDER BY m.recorded_at DESC LIMIT ?"
    with closing(connect(db_path)) as conn:
        rows = conn.execute(sql, (*params, limit)).fetchall()
        return _summarize(conn, rows, terms)


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


def memories_by_tag(tag: str, limit: int = DEFAULT_LIMIT, db_path=None) -> list[dict]:
    """Memories carrying exactly this tag, newest first."""
    tag = _clean(tag, "tag", MAX_TAG_LEN, True)
    limit = _clean_limit(limit)
    with closing(connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT m.* FROM memories m WHERE m.id IN (SELECT mt.memory_id FROM memory_tags mt "
            "JOIN tags t ON t.id = mt.tag_id WHERE t.name = ?) "
            "ORDER BY m.recorded_at DESC, m.created_at DESC LIMIT ?",
            (tag, limit),
        ).fetchall()
        return _summarize(conn, rows)


def pending_memories(days: int = DEFAULT_PENDING_DAYS, limit: int = DEFAULT_LIMIT, db_path=None,
                     exclude_suggested: bool = False) -> list[dict]:
    """Memories recorded in the last `days` days that have no registrations row yet, newest first.

    With exclude_suggested, memories already mailed by the proposal-only run are left out too."""
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise ValidationError("days must be a positive integer")
    days = min(days, MAX_PENDING_DAYS)
    limit = _clean_limit(limit)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with closing(connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT m.* FROM memories m WHERE julianday(m.recorded_at) >= julianday(?) "
            "AND NOT EXISTS (SELECT 1 FROM registrations r WHERE r.memory_id = m.id) "
            + ("AND NOT EXISTS (SELECT 1 FROM suggestions s WHERE s.memory_id = m.id) " if exclude_suggested else "")
            + "ORDER BY m.recorded_at DESC, m.created_at DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        return _summarize(conn, rows)


def record_registration(memory_id: str, kind: str, external_id: str | None = None, db_path=None) -> dict:
    """Record that a memory was reviewed: an event/task created elsewhere (external_id required) or 'none'."""
    memory_id = _clean(memory_id, "memory_id", 64, True)
    kind = _clean(kind, "kind", 10, True)
    if kind not in REGISTRATION_KINDS:
        raise ValidationError(f"kind must be one of {', '.join(REGISTRATION_KINDS)}")
    external_id = _clean(external_id, "external_id", 200, required=kind != "none") or None
    if kind == "none" and external_id:
        raise ValidationError("external_id must be empty when kind is 'none'")
    with closing(connect(db_path)) as conn:
        with conn:
            if conn.execute("SELECT 1 FROM memories WHERE id = ?", (memory_id,)).fetchone() is None:
                raise NotFoundError(f"memory not found: {memory_id}")
            conn.execute(
                "INSERT INTO registrations (memory_id, kind, external_id, created_at) VALUES (?,?,?,?)",
                (memory_id, kind, external_id, _now()),
            )
    return {"memory_id": memory_id, "kind": kind, "external_id": external_id}


def mark_suggested(memory_ids: list[str], db_path=None) -> int:
    """Record that these memories were mailed as candidates. Unknown ids and repeats are ignored."""
    now = _now()
    with closing(connect(db_path)) as conn:
        with conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO suggestions (memory_id, suggested_at) "
                "SELECT id, ? FROM memories WHERE id = ?",
                [(now, i) for i in memory_ids],
            )
            return conn.total_changes - before


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
