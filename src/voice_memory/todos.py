"""TODOs the user has adopted, stored next to the memories they came from.

Only call add_todo for something the user explicitly decided to do. A candidate that a model merely proposed
is not a TODO and is not stored. Deadlines are never inferred: due_date is a concrete YYYY-MM-DD the user gave,
otherwise it stays NULL and the user's own wording (if any) goes in due_text.
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
import uuid
from contextlib import closing
from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import database as db
from .database import NotFoundError, ValidationError, _clean, _now

STATUSES = ("open", "in_progress", "completed", "cancelled", "dismissed")
ACTIVE = ("open", "in_progress")
PRIORITIES = ("high", "medium", "low")
MAX_TODO_TITLE = 200
MAX_DETAIL = 2_000
MAX_DUE_TEXT = 200
MAX_LINKS = 20
DEFAULT_LIMIT = 20
JST = ZoneInfo("Asia/Tokyo")
_UNSET = object()


def title_key(title: str) -> str:
    """Normalized title used to spot the same TODO written twice (width, case, spacing and punctuation ignored)."""
    t = unicodedata.normalize("NFKC", title).casefold()
    return re.sub(r"[\s\W_]+", "", t)


def _clean_due_date(value) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        raise ValidationError("due_date must be YYYY-MM-DD (leave it empty if the date is not decided)")
    try:
        date.fromisoformat(value.strip())
    except ValueError:
        raise ValidationError("due_date is not a real date") from None
    return value.strip()


def _clean_priority(value) -> str | None:
    if value is None or value == "":
        return None
    if value not in PRIORITIES:
        raise ValidationError(f"priority must be one of {', '.join(PRIORITIES)}")
    return value


def _clean_status(value) -> str:
    if value not in STATUSES:
        raise ValidationError(f"status must be one of {', '.join(STATUSES)}")
    return value


def _clean_memory_ids(ids) -> list[str]:
    if ids is None:
        return []
    if not isinstance(ids, list):
        raise ValidationError("memory_ids must be a list of strings")
    if len(ids) > MAX_LINKS:
        raise ValidationError(f"at most {MAX_LINKS} memory ids are allowed")
    out: list[str] = []
    for i in ids:
        i = _clean(i, "memory_id", 64, True)
        if i not in out:
            out.append(i)
    return out


def _link(conn: sqlite3.Connection, todo_id: str, memory_ids: list[str], now: str) -> None:
    for mid in memory_ids:
        if conn.execute("SELECT 1 FROM memories WHERE id = ?", (mid,)).fetchone() is None:
            raise NotFoundError(f"memory not found: {mid}")
        conn.execute("INSERT OR IGNORE INTO todo_memories (todo_id, memory_id, added_at) VALUES (?,?,?)",
                     (todo_id, mid, now))


def _row(conn: sqlite3.Connection, todo_id: str, with_memories: bool = False) -> dict:
    r = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
    if r is None:
        raise NotFoundError(f"todo not found: {todo_id}")
    out = {k: r[k] for k in r.keys() if k != "title_key"}
    out["overdue"] = bool(out["due_date"] and out["status"] in ACTIVE and out["due_date"] < _today())
    mems = conn.execute(
        "SELECT m.id, m.recorded_at, m.title FROM todo_memories tm JOIN memories m ON m.id = tm.memory_id "
        "WHERE tm.todo_id = ? ORDER BY m.recorded_at", (todo_id,)).fetchall()
    out["memory_ids"] = [m["id"] for m in mems]
    if with_memories:
        out["memories"] = [dict(m) for m in mems]
    return out


def _today() -> str:
    return datetime.now(JST).date().isoformat()


def add_todo(title: str, detail: str = "", due_date: str | None = None, due_text: str | None = None,
             priority: str | None = None, memory_ids: list[str] | None = None, db_path=None) -> dict:
    """Store a TODO the user adopted. If an active TODO with the same normalized title exists, nothing new is
    created: the given memory ids are linked to it and it is returned with duplicate=True."""
    title = _clean(title, "title", MAX_TODO_TITLE, True)
    key = title_key(title)
    if not key:
        raise ValidationError("title must contain letters or digits")
    detail = _clean(detail, "detail", MAX_DETAIL, False)
    due_date = _clean_due_date(due_date)
    due_text = _clean(due_text, "due_text", MAX_DUE_TEXT, False) or None
    priority = _clean_priority(priority)
    ids = _clean_memory_ids(memory_ids)
    now = _now()
    with closing(db.connect(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")  # the check and the insert must not interleave with another writer
        try:
            existing = conn.execute(
                "SELECT id FROM todos WHERE title_key = ? AND status IN ('open', 'in_progress')", (key,)).fetchone()
            if existing:
                todo_id, duplicate = existing["id"], True
                _link(conn, todo_id, ids, now)
                conn.execute("UPDATE todos SET last_reviewed_at = ? WHERE id = ?", (now, todo_id))
            else:
                todo_id, duplicate = str(uuid.uuid4()), False
                conn.execute(
                    "INSERT INTO todos (id, title, title_key, detail, status, priority, due_date, due_text, "
                    "created_at, updated_at, last_reviewed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (todo_id, title, key, detail, "open", priority, due_date, due_text, now, now, now))
                _link(conn, todo_id, ids, now)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return {**_row(conn, todo_id), "duplicate": duplicate}


def get_todo(todo_id: str, db_path=None) -> dict:
    todo_id = _clean(todo_id, "todo_id", 64, True)
    with closing(db.connect(db_path)) as conn:
        return _row(conn, todo_id, with_memories=True)


def list_todos(status: str = "active", limit: int = DEFAULT_LIMIT, overdue_only: bool = False,
               db_path=None) -> list[dict]:
    """status: 'active' (open + in_progress, default), 'all', or one status. Active ones first by due date
    (undated last), then oldest first."""
    if status == "active":
        statuses = ACTIVE
    elif status == "all":
        statuses = STATUSES
    else:
        statuses = (_clean_status(status),)
    limit = db._clean_limit(limit)
    marks = ",".join("?" * len(statuses))
    sql = f"SELECT id FROM todos WHERE status IN ({marks})"
    args: list = list(statuses)
    if overdue_only:
        sql += " AND due_date IS NOT NULL AND due_date < ? AND status IN ('open', 'in_progress')"
        args.append(_today())
    sql += " ORDER BY due_date IS NULL, due_date, created_at LIMIT ?"
    args.append(limit)
    with closing(db.connect(db_path)) as conn:
        return [_row(conn, r["id"]) for r in conn.execute(sql, args).fetchall()]


def update_todo(todo_id: str, title=_UNSET, detail=_UNSET, due_date=_UNSET, due_text=_UNSET, priority=_UNSET,
                add_memory_ids: list[str] | None = None, reviewed: bool = False, status=_UNSET,
                db_path=None) -> dict:
    """Change the given fields only (omitted = unchanged; '' clears due_date / due_text / priority).
    reviewed=True records that the user looked at it without changing anything else."""
    todo_id = _clean(todo_id, "todo_id", 64, True)
    sets: dict[str, object] = {}
    if title is not _UNSET:
        sets["title"] = _clean(title, "title", MAX_TODO_TITLE, True)
        sets["title_key"] = title_key(sets["title"])
        if not sets["title_key"]:
            raise ValidationError("title must contain letters or digits")
    if detail is not _UNSET:
        sets["detail"] = _clean(detail, "detail", MAX_DETAIL, False)
    if due_date is not _UNSET:
        sets["due_date"] = _clean_due_date(due_date)
    if due_text is not _UNSET:
        sets["due_text"] = _clean(due_text, "due_text", MAX_DUE_TEXT, False) or None
    if priority is not _UNSET:
        sets["priority"] = _clean_priority(priority)
    if status is not _UNSET:
        sets["status"] = _clean_status(status)
    ids = _clean_memory_ids(add_memory_ids)
    now = _now()
    with closing(db.connect(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute("SELECT status FROM todos WHERE id = ?", (todo_id,)).fetchone()
            if cur is None:
                raise NotFoundError(f"todo not found: {todo_id}")
            if "status" in sets and sets["status"] != cur["status"]:
                sets["completed_at"] = now if sets["status"] == "completed" else None
            _link(conn, todo_id, ids, now)
            if sets or ids or reviewed:
                sets["updated_at"] = now
                if reviewed:
                    sets["last_reviewed_at"] = now
                cols = ", ".join(f"{k} = ?" for k in sets)
                try:
                    conn.execute(f"UPDATE todos SET {cols} WHERE id = ?", [*sets.values(), todo_id])
                except sqlite3.IntegrityError:
                    raise ValidationError("another active TODO already has this title") from None
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return _row(conn, todo_id)


def set_todo_status(todo_id: str, status: str, db_path=None) -> dict:
    """Move a TODO to open / in_progress / completed / cancelled / dismissed. completed_at is set on completion
    and cleared when it leaves 'completed'."""
    return update_todo(todo_id, status=status, db_path=db_path)


def todos_for_memories(conn: sqlite3.Connection, memory_ids: list[str]) -> list[dict]:
    """TODOs linked to any of these memories, active ones first. For the mail digest."""
    if not memory_ids:
        return []
    marks = ",".join("?" * len(memory_ids))
    rows = conn.execute(
        f"SELECT DISTINCT t.id, t.title, t.status, t.due_date, t.due_text FROM todos t "
        f"JOIN todo_memories tm ON tm.todo_id = t.id WHERE tm.memory_id IN ({marks}) "
        f"ORDER BY t.status NOT IN ('open', 'in_progress'), t.due_date IS NULL, t.due_date, t.created_at",
        memory_ids).fetchall()
    return [dict(r) for r in rows]
