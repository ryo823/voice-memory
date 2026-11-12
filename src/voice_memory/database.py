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
# Asia/Tokyo has no DST, so a fixed +9h shift converts any stored ISO offset to the JST calendar day.
# SQLite date()/datetime() first normalise the stored offset to UTC, so this works for +00:00 and +09:00 rows alike.
JST_SHIFT = "+9 hours"

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id          TEXT PRIMARY KEY,
    recorded_at TEXT NOT NULL,
    title       TEXT NOT NULL,
    summary     TEXT NOT NULL DEFAULT '',
    transcript  TEXT NOT NULL,
    audio_path  TEXT,
    source      TEXT NOT NULL DEFAULT 'manual',
    created_at  TEXT NOT NULL,
    audio_start_sec  REAL,
    audio_end_sec    REAL,
    parent_memory_id TEXT REFERENCES memories(id),
    split_method     TEXT,
    archived         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_memories_recorded_at ON memories(recorded_at DESC);
-- One audio file -> several memories (split by silence gaps). file_sha256 is ingested_files.sha256.
CREATE TABLE IF NOT EXISTS ingested_file_memories (
    file_sha256   TEXT    NOT NULL,
    memory_id     TEXT    NOT NULL PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    segment_index INTEGER NOT NULL,
    UNIQUE (file_sha256, segment_index)
);
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
-- TODOs the user has explicitly adopted (see todos.py). Candidates proposed by a model are never stored here.
-- title_key is the normalized title; two active (open / in_progress) TODOs cannot share it.
-- due_date is only set when the user gave a concrete date; a vague deadline stays in due_text, due_date NULL.
CREATE TABLE IF NOT EXISTS todos (
    id               TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    title_key        TEXT NOT NULL,
    detail           TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'open'
                     CHECK (status IN ('open', 'in_progress', 'completed', 'cancelled', 'dismissed')),
    priority         TEXT CHECK (priority IS NULL OR priority IN ('high', 'medium', 'low')),
    due_date         TEXT,
    due_text         TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    completed_at     TEXT,
    last_reviewed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_todos_active_title
    ON todos(title_key) WHERE status IN ('open', 'in_progress');
CREATE INDEX IF NOT EXISTS idx_todos_status_due ON todos(status, due_date);
-- Source memories of a TODO. Deleting a memory removes only the link, never the TODO.
CREATE TABLE IF NOT EXISTS todo_memories (
    todo_id   TEXT NOT NULL REFERENCES todos(id)    ON DELETE CASCADE,
    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    added_at  TEXT NOT NULL,
    PRIMARY KEY (todo_id, memory_id)
);
CREATE INDEX IF NOT EXISTS idx_todo_memories_memory ON todo_memories(memory_id);
-- Log of follow-up reminders about TODOs that were mailed (see followup.py). `basis` says what the reminder was
-- about (the due date, or the last-activity time), so changing the due date or touching the TODO starts afresh.
CREATE TABLE IF NOT EXISTS todo_notifications (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    todo_id TEXT NOT NULL REFERENCES todos(id) ON DELETE CASCADE,
    reason  TEXT NOT NULL CHECK (reason IN ('overdue', 'due_soon', 'stale', 'undated')),
    basis   TEXT NOT NULL,
    sent_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_todo_notifications ON todo_notifications(todo_id, reason, basis);
-- Keyed by memory_id, not memories.rowid: memories has a TEXT primary key, so VACUUM may renumber rowids.
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    memory_id UNINDEXED, title, summary, transcript, tags, tokenize = 'trigram'
);
"""

# trigram FTS cannot match terms shorter than this many characters; those use LIKE.
FTS_MIN_TERM = 3

# Result ordering for search_memories: "recent" (newest first) or "bm25" (relevance, ties newest first).
# Override per call with `order=` or globally with the VOICE_MEMORY_SEARCH_ORDER env var; set it to
# "recent" to roll back. bm25 only applies when the query has a term of FTS_MIN_TERM+ characters: queries made
# only of shorter terms use LIKE, which has no score, so they are always newest first.
SEARCH_ORDERS = ("recent", "bm25")
DEFAULT_SEARCH_ORDER = "bm25"
# bm25() column weights in memories_fts column order (memory_id, title, summary, transcript, tags).
# Title counts most, then summary, tags, transcript. memory_id is UNINDEXED so its weight is unused.
BM25_WEIGHTS = {"memory_id": 0.0, "title": 10.0, "summary": 5.0, "transcript": 1.0, "tags": 3.0}


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


# Columns added after the first release; ALTER TABLE ADD COLUMN only, so existing rows are untouched.
MEMORY_MIGRATION_COLUMNS = (
    ("audio_start_sec", "REAL"),
    ("audio_end_sec", "REAL"),
    ("parent_memory_id", "TEXT REFERENCES memories(id)"),
    ("split_method", "TEXT"),
    ("archived", "INTEGER NOT NULL DEFAULT 0"),
)


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Add the conversation-split columns to an older `memories` table. Idempotent; returns what it added."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(memories)")}
    added = []
    for name, decl in MEMORY_MIGRATION_COLUMNS:
        if name not in have:
            try:
                conn.execute(f"ALTER TABLE memories ADD COLUMN {name} {decl}")
                added.append(name)
            except sqlite3.OperationalError as e:  # another process added it between the check and the ALTER
                if "duplicate column" not in str(e):
                    raise
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_parent ON memories(parent_memory_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_archived ON memories(archived)")
    conn.commit()
    return added


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create missing tables, then migrate. Safe to call repeatedly."""
    conn.executescript(SCHEMA)
    conn.commit()
    migrate(conn)


def init_db(db_path: Path | str | None = None) -> None:
    """Create tables if missing and migrate older DBs. Safe to call repeatedly."""
    with closing(connect(db_path)) as conn:
        ensure_schema(conn)
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


def _clean_offset(offset: int) -> int:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValidationError("offset must be a non-negative integer")
    return offset


def _clean_day(day: str) -> str:
    """Validate a YYYY-MM-DD calendar day and return it normalised."""
    if not isinstance(day, str):
        raise ValidationError("date must be YYYY-MM-DD")
    try:
        return datetime.strptime(day, "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise ValidationError("date must be YYYY-MM-DD") from None


def _clean_month(month: str) -> str:
    """Validate a YYYY-MM month and return it normalised."""
    if not isinstance(month, str):
        raise ValidationError("month must be YYYY-MM")
    try:
        return datetime.strptime(month, "%Y-%m").strftime("%Y-%m")
    except ValueError:
        raise ValidationError("month must be YYYY-MM") from None


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
    *,
    audio_start_sec: float | None = None,
    audio_end_sec: float | None = None,
    parent_memory_id: str | None = None,
    split_method: str | None = None,
    file_sha256: str | None = None,
    segment_index: int | None = None,
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
            "INSERT INTO memories (id, recorded_at, title, summary, transcript, audio_path, source, created_at, "
            "audio_start_sec, audio_end_sec, parent_memory_id, split_method) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (memory_id, recorded_at, title, summary, transcript, audio_path, source, _now(),
             audio_start_sec, audio_end_sec, parent_memory_id, split_method),
        )
        if file_sha256 is not None:
            c.execute("INSERT INTO ingested_file_memories (file_sha256, memory_id, segment_index) VALUES (?,?,?)",
                      (file_sha256, memory_id, segment_index or 0))
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


def _search_order(order: str | None) -> str:
    order = order or os.environ.get("VOICE_MEMORY_SEARCH_ORDER") or DEFAULT_SEARCH_ORDER
    if order not in SEARCH_ORDERS:
        raise ValidationError(f"order must be one of {', '.join(SEARCH_ORDERS)}")
    return order


def search_memories(query: str, limit: int = DEFAULT_LIMIT, db_path=None, offset: int = 0,
                    order: str | None = None, include_archived: bool = False,
                    on_date: str | None = None) -> list[dict]:
    """AND search over whitespace-separated terms in title, summary, transcript and tags.

    Terms of FTS_MIN_TERM+ characters use the trigram FTS index; shorter ones fall back
    to a literal LIKE. `order` is "recent" or "bm25" (default: env / DEFAULT_SEARCH_ORDER);
    bm25 ranks by relevance, ties newest first. Queries with only short (LIKE) terms have no
    score and stay newest first whatever `order` says.
    """
    order = _search_order(order)
    query = _clean(query, "query", MAX_QUERY, True)
    limit = _clean_limit(limit)
    offset = _clean_offset(offset)
    terms = list(dict.fromkeys(query.split()))
    long_terms = [t for t in terms if len(t) >= FTS_MIN_TERM]
    short_terms = [t for t in terms if len(t) < FTS_MIN_TERM]

    where: list[str] = [] if include_archived else ["m.archived = 0"]
    params: list = []
    join = ""
    ranking = ""
    if long_terms:
        if order == "bm25":
            weights = ", ".join(str(w) for w in BM25_WEIGHTS.values())
            join = "JOIN memories_fts ON memories_fts.memory_id = m.id"
            where.append("memories_fts MATCH ?")
            ranking = f"bm25(memories_fts, {weights}), "
        else:
            where.append("m.id IN (SELECT memory_id FROM memories_fts WHERE memories_fts MATCH ?)")
        params.append(" AND ".join(_fts_term(t) for t in long_terms))
    for t in short_terms:
        where.append(
            "(m.title LIKE ? ESCAPE '\\' OR m.summary LIKE ? ESCAPE '\\' OR m.transcript LIKE ? ESCAPE '\\' "
            "OR EXISTS (SELECT 1 FROM memory_tags mt JOIN tags t ON t.id = mt.tag_id "
            "WHERE mt.memory_id = m.id AND t.name LIKE ? ESCAPE '\\'))"
        )
        params += [f"%{_like_escape(t)}%"] * 4
    if on_date is not None:
        # A single JST day is read as a conversation timeline, oldest first, not by relevance.
        where.append(f"date(m.recorded_at, '{JST_SHIFT}') = ?")
        params.append(_clean_day(on_date))
        ranking = ""
        order_clause = "m.recorded_at ASC, m.created_at ASC"
    else:
        order_clause = f"{ranking}m.recorded_at DESC, m.created_at DESC"
    sql = (f"SELECT m.* FROM memories m {join} WHERE {' AND '.join(where)} "
           f"ORDER BY {order_clause} LIMIT ? OFFSET ?")
    with closing(connect(db_path)) as conn:
        rows = conn.execute(sql, (*params, limit, offset)).fetchall()
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


def delete_memory(memory_id: str, recordings: Path | str | None = None, db_path=None) -> dict:
    """Permanently delete one memory: its row, tags links, registrations, FTS entry and (if no other memory
    shares it) the stored audio copy under `recordings`.

    The ingest record is kept as a tombstone (status 'deleted', transcript wiped) so the same source file
    sitting in the inbox / on the Mac is not ingested again. Returns {"id", "audio_deleted"}.
    """
    memory_id = _clean(memory_id, "id", 64, True)
    with closing(connect(db_path)) as conn:
        with conn:  # one transaction
            row = conn.execute("SELECT audio_path FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"memory not found: {memory_id}")
            audio_path = row["audio_path"]
            conn.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'ingested_files'").fetchone():
                # A file split into several memories stays ingested until its last live memory is deleted.
                sibling = conn.execute(
                    "SELECT s.memory_id FROM ingested_file_memories s JOIN ingested_file_memories me "
                    "ON me.file_sha256 = s.file_sha256 WHERE me.memory_id = ? AND s.memory_id != ? "
                    "ORDER BY s.segment_index LIMIT 1", (memory_id, memory_id)).fetchone()
                if sibling is None:
                    conn.execute(
                        "UPDATE ingested_files SET status = 'deleted', transcript = NULL, error = NULL, "
                        "memory_id = NULL WHERE memory_id = ?", (memory_id,))
                else:
                    conn.execute("UPDATE ingested_files SET memory_id = ? WHERE memory_id = ?",
                                 (sibling["memory_id"], memory_id))
            conn.execute("UPDATE memories SET parent_memory_id = NULL WHERE parent_memory_id = ?", (memory_id,))
            conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))  # cascades tag links etc.
            conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM memory_tags)")
            shared = audio_path and conn.execute(
                "SELECT 1 FROM memories WHERE audio_path = ?", (audio_path,)).fetchone()
    audio_deleted = False
    if audio_path and not shared:
        if recordings is None:
            from .ingest.config import recordings_dir
            recordings = recordings_dir()
        root = Path(recordings).resolve()
        target = (root / audio_path).resolve()
        if root in target.parents and target.is_file():  # never follow a path out of the recordings dir
            target.unlink()
            audio_deleted = True
    return {"id": memory_id, "audio_deleted": audio_deleted}


def recent_memories(limit: int = DEFAULT_LIMIT, db_path=None, offset: int = 0,
                    include_archived: bool = False) -> list[dict]:
    limit = _clean_limit(limit)
    offset = _clean_offset(offset)
    with closing(connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM memories " + ("" if include_archived else "WHERE archived = 0 ")
            + "ORDER BY recorded_at DESC, created_at DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
        return _summarize(conn, rows)


def memories_by_tag(tag: str, limit: int = DEFAULT_LIMIT, db_path=None, offset: int = 0,
                    on_date: str | None = None) -> list[dict]:
    """Memories carrying exactly this tag, newest first (oldest first when restricted to one JST day)."""
    tag = _clean(tag, "tag", MAX_TAG_LEN, True)
    limit = _clean_limit(limit)
    offset = _clean_offset(offset)
    date_cond, params = "", [tag]
    order_clause = "m.recorded_at DESC, m.created_at DESC"
    if on_date is not None:
        date_cond = f" AND date(m.recorded_at, '{JST_SHIFT}') = ?"
        params.append(_clean_day(on_date))
        order_clause = "m.recorded_at ASC, m.created_at ASC"
    with closing(connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT m.* FROM memories m WHERE m.archived = 0 AND m.id IN (SELECT mt.memory_id FROM memory_tags mt "
            "JOIN tags t ON t.id = mt.tag_id WHERE t.name = ?)" + date_cond + f" "
            f"ORDER BY {order_clause} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return _summarize(conn, rows)


def memories_by_date(day: str, limit: int = DEFAULT_LIMIT, db_path=None, offset: int = 0) -> list[dict]:
    """Memories recorded on one Asia/Tokyo calendar day, oldest first (conversation order)."""
    day = _clean_day(day)
    limit = _clean_limit(limit)
    offset = _clean_offset(offset)
    with closing(connect(db_path)) as conn:
        rows = conn.execute(
            f"SELECT * FROM memories WHERE archived = 0 AND date(recorded_at, '{JST_SHIFT}') = ? "
            "ORDER BY recorded_at ASC, created_at ASC LIMIT ? OFFSET ?",
            (day, limit, offset),
        ).fetchall()
        return _summarize(conn, rows)


def daily_counts(month: str, db_path=None) -> dict[str, int]:
    """Non-archived memory counts per Asia/Tokyo day for one YYYY-MM month, as {day: count}."""
    month = _clean_month(month)
    with closing(connect(db_path)) as conn:
        rows = conn.execute(
            f"SELECT date(recorded_at, '{JST_SHIFT}') AS day, COUNT(*) AS count FROM memories "
            f"WHERE archived = 0 AND strftime('%Y-%m', date(recorded_at, '{JST_SHIFT}')) = ? GROUP BY day",
            (month,),
        ).fetchall()
        return {r["day"]: r["count"] for r in rows}


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
            "SELECT m.* FROM memories m WHERE julianday(m.recorded_at) >= julianday(?) AND m.archived = 0 "
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
            "SELECT t.name, COUNT(m.id) AS count FROM tags t "
            "LEFT JOIN memory_tags mt ON mt.tag_id = t.id "
            "LEFT JOIN memories m ON m.id = mt.memory_id AND m.archived = 0 GROUP BY t.id "
            "ORDER BY count DESC, t.name"
        ).fetchall()
        return [dict(r) for r in rows]


def init_cli() -> None:
    init_db()
    print(f"initialized: {default_db_path()}")
