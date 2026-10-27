"""Follow-up reminders about adopted TODOs, added to the daily mail.

Each active (open / in_progress) TODO gets at most one reason, the first that applies:
  overdue   due_date has passed
  due_soon  due_date is within DUE_SOON_DAYS days (today included)
  stale     no update or review for STALE_DAYS days
  undated   no due_date and created UNDATED_DAYS or more days ago
A reminder is skipped when the same (todo, reason, basis) was already mailed, where basis is the due date
(overdue / due_soon) or the last-activity time (stale), so a changed due date or a touched TODO starts afresh.
overdue and stale are repeated after OVERDUE_REMIND_DAYS / STALE_DAYS (0 = never repeat); due_soon and undated
are sent once. Completed, cancelled and dismissed TODOs are never reminded. Nothing is recorded here: the caller
records the reminders with record() only after the mail was sent, so a failed run retries them.

Thresholds (env): VOICE_MEMORY_TODO_DUE_SOON_DAYS=3, _UNDATED_DAYS=14, _STALE_DAYS=30, _OVERDUE_REMIND_DAYS=7.
"""
from __future__ import annotations

import os
from contextlib import closing
from datetime import date, datetime, timedelta, timezone

from . import database as db, todos

REASONS = ("overdue", "due_soon", "stale", "undated")
LABELS = {"overdue": "期限超過", "due_soon": "期限間近", "stale": "長期間更新なし", "undated": "期限未設定のまま経過"}
MAX_NOTICES = 20
MAX_LINKED = 3

SECTION_HEADER = (
    "■■ 採用済みTODOの確認\n"
    "以下はユーザーが採用済みのTODOで、期限や更新状況から確認が必要なものです(新しい候補ではありません)。"
    "必要なら、完了・取り消し・期限の見直し・優先順位・次の行動を提案してください。"
    "期限が未設定または日付未確定のものは、推測で期限を決めず確認してください。完了にするかどうかはユーザーが決めます。"
)


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


def settings() -> dict:
    return {"due_soon": _env_int("VOICE_MEMORY_TODO_DUE_SOON_DAYS", 3),
            "undated": _env_int("VOICE_MEMORY_TODO_UNDATED_DAYS", 14),
            "stale": _env_int("VOICE_MEMORY_TODO_STALE_DAYS", 30),
            "overdue_remind": _env_int("VOICE_MEMORY_TODO_OVERDUE_REMIND_DAYS", 7)}


def _ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _classify(t: dict, now: datetime, cfg: dict) -> tuple[str, str] | None:
    """(reason, basis) for an active TODO, or None when nothing applies."""
    today = now.astimezone(todos.JST).date()
    if t["due_date"]:
        left = (date.fromisoformat(t["due_date"]) - today).days
        if left < 0:
            return "overdue", t["due_date"]
        if left <= cfg["due_soon"]:
            return "due_soon", t["due_date"]
    last = max(_ts(t[k]) for k in ("created_at", "updated_at", "last_reviewed_at") if t[k])
    if cfg["stale"] and now - last >= timedelta(days=cfg["stale"]):
        return "stale", last.isoformat(timespec="seconds")
    if not t["due_date"] and cfg["undated"] and now - _ts(t["created_at"]) >= timedelta(days=cfg["undated"]):
        return "undated", "undated"
    return None


def _repeat_days(reason: str, cfg: dict) -> int:
    return {"overdue": cfg["overdue_remind"], "stale": cfg["stale"]}.get(reason, 0)


def pending_notices(db_path=None, now: datetime | None = None, limit: int = MAX_NOTICES) -> list[dict]:
    """Reminders due now, most urgent first: [{todo, reason, basis, days}]. Reads only."""
    now = now or datetime.now(timezone.utc)
    cfg = settings()
    out = []
    with closing(db.connect(db_path)) as conn:
        for r in conn.execute("SELECT id FROM todos WHERE status IN ('open', 'in_progress')").fetchall():
            t = todos._row(conn, r["id"])
            hit = _classify(t, now, cfg)
            if hit is None:
                continue
            reason, basis = hit
            last = conn.execute(
                "SELECT MAX(sent_at) AS s FROM todo_notifications WHERE todo_id = ? AND reason = ? AND basis = ?",
                (t["id"], reason, basis)).fetchone()["s"]
            if last is not None:
                repeat = _repeat_days(reason, cfg)
                if not repeat or now - _ts(last) < timedelta(days=repeat):
                    continue
            linked = conn.execute(
                "SELECT m.id, m.recorded_at, m.title FROM todo_memories tm JOIN memories m ON m.id = tm.memory_id "
                "WHERE tm.todo_id = ? ORDER BY m.recorded_at DESC LIMIT ?", (t["id"], MAX_LINKED)).fetchall()
            out.append({"todo": t, "reason": reason, "basis": basis, "linked": [dict(m) for m in linked]})
    out.sort(key=lambda n: (REASONS.index(n["reason"]), n["todo"]["due_date"] or "9999", n["todo"]["created_at"]))
    return out[:limit]


def record(notices: list[dict], db_path=None, now: datetime | None = None) -> int:
    """Log reminders as mailed. Call only after the mail was sent."""
    sent_at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    with closing(db.connect(db_path)) as conn:
        with conn:
            conn.executemany(
                "INSERT INTO todo_notifications (todo_id, reason, basis, sent_at) VALUES (?,?,?,?)",
                [(n["todo"]["id"], n["reason"], n["basis"], sent_at) for n in notices])
    return len(notices)


def _one_line(text: str) -> str:
    return " ".join(text.split())


def format_section(notices: list[dict], now: datetime | None = None) -> str:
    """Mail section for the reminders ('' when there are none)."""
    if not notices:
        return ""
    from .digest import format_recorded
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(todos.JST).date()
    lines = [SECTION_HEADER]
    for n in notices:
        t = n["todo"]
        if t["due_date"]:
            days = (date.fromisoformat(t["due_date"]) - today).days
            due = f"{t['due_date']}(" + (f"{-days}日超過" if days < 0 else "今日" if days == 0 else f"あと{days}日") + ")"
        else:
            due = "日付未確定" if t["due_text"] else "未設定"
        if t["due_text"]:
            due += f" 本人の表現「{_one_line(t['due_text'])}」"
        last = max(_ts(t[k]) for k in ("created_at", "updated_at", "last_reviewed_at") if t[k])
        lines.append(f"- TODO ID: {t['id']} [{t['status']}] {_one_line(t['title'])}\n"
                     f"    理由: {LABELS[n['reason']]} / 期限: {due} / 優先順位: {t['priority'] or '不明'} / "
                     f"登録: {t['created_at'][:10]} / 最終更新・確認: {last.astimezone(todos.JST):%Y-%m-%d}")
        for m in n["linked"]:
            lines.append(f"    関連記録: {m['id']} ({format_recorded(m['recorded_at'])[:10]}) {_one_line(m['title'])}")
    return "\n".join(lines)
