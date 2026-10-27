import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from voice_memory import database as db, followup, suggest, todos
from test_database import _days_ago, save

NOW = datetime(2026, 11, 10, 3, 0, tzinfo=timezone.utc)  # 12:00 JST on 2026-11-10


def age(db_path, todo_id, days):
    """Make a TODO look `days` old (created, updated and reviewed)."""
    ts = (NOW - timedelta(days=days)).isoformat(timespec="seconds")
    with closing(db.connect(db_path)) as c, c:
        c.execute("UPDATE todos SET created_at=?, updated_at=?, last_reviewed_at=? WHERE id=?",
                  (ts, ts, ts, todo_id))


def make(db_path, title, due=None, days_old=1, **kw):
    t = todos.add_todo(title, due_date=due, db_path=db_path, **kw)
    age(db_path, t["id"], days_old)
    return t


def reasons(db_path, now=NOW):
    return {n["todo"]["title"]: n["reason"] for n in followup.pending_notices(db_path, now)}


def test_each_reason_and_nothing_for_quiet_todos(db_path):
    make(db_path, "超過", "2026-11-09")
    make(db_path, "今日", "2026-11-10")
    make(db_path, "3日後", "2026-11-13")
    make(db_path, "4日後", "2026-11-14")
    make(db_path, "古い", "2026-12-31", days_old=30)
    make(db_path, "期限なし古い", None, days_old=14)
    make(db_path, "期限なし新しい", None, days_old=13)
    assert reasons(db_path) == {"超過": "overdue", "今日": "due_soon", "3日後": "due_soon",
                                "古い": "stale", "期限なし古い": "undated"}


def test_overdue_beats_other_reasons_and_one_line_per_todo(db_path):
    make(db_path, "超過かつ古い", "2026-10-01", days_old=60)
    ns = followup.pending_notices(db_path, NOW)
    assert [n["reason"] for n in ns] == ["overdue"]


@pytest.mark.parametrize("status", ["completed", "cancelled", "dismissed"])
def test_closed_todos_are_never_reminded(db_path, status):
    t = make(db_path, "終わった", "2026-11-01", days_old=60)
    todos.set_todo_status(t["id"], status, db_path)
    age(db_path, t["id"], 60)
    assert followup.pending_notices(db_path, NOW) == []


def test_in_progress_is_included(db_path):
    t = make(db_path, "対応中", "2026-11-01")
    todos.set_todo_status(t["id"], "in_progress", db_path)
    assert reasons(db_path) == {"対応中": "overdue"}


def test_no_duplicate_after_record_and_due_change_starts_afresh(db_path):
    t = make(db_path, "締切", "2026-11-13")
    followup.record(followup.pending_notices(db_path, NOW), db_path, NOW)
    assert followup.pending_notices(db_path, NOW + timedelta(hours=20)) == []
    assert followup.pending_notices(db_path, NOW + timedelta(days=1)) == []  # due_soon is sent once per due date
    todos.update_todo(t["id"], due_date="2026-11-12", db_path=db_path)
    age(db_path, t["id"], 1)
    assert reasons(db_path) == {"締切": "due_soon"}


def test_overdue_repeats_weekly_not_daily(db_path, monkeypatch):
    make(db_path, "超過", "2026-11-01")
    followup.record(followup.pending_notices(db_path, NOW), db_path, NOW)
    assert followup.pending_notices(db_path, NOW + timedelta(days=6)) == []
    assert len(followup.pending_notices(db_path, NOW + timedelta(days=7))) == 1
    monkeypatch.setenv("VOICE_MEMORY_TODO_OVERDUE_REMIND_DAYS", "0")
    assert followup.pending_notices(db_path, NOW + timedelta(days=30)) == []


def test_undated_is_sent_once_and_review_resets_stale(db_path):
    t = make(db_path, "宙ぶらりん", None, days_old=20)
    followup.record(followup.pending_notices(db_path, NOW), db_path, NOW)
    assert followup.pending_notices(db_path, NOW + timedelta(days=5)) == []
    s = make(db_path, "放置", "2027-01-01", days_old=40)
    assert reasons(db_path) == {"放置": "stale"}
    todos.update_todo(s["id"], reviewed=True, db_path=db_path)  # touched "now" in real time, far from NOW
    ts = NOW.isoformat(timespec="seconds")
    with closing(db.connect(db_path)) as c, c:
        c.execute("UPDATE todos SET updated_at=?, last_reviewed_at=? WHERE id=?", (ts, ts, s["id"]))
    assert "放置" not in reasons(db_path)


def test_thresholds_from_env(db_path, monkeypatch):
    make(db_path, "5日後", "2026-11-15")
    assert reasons(db_path) == {}
    monkeypatch.setenv("VOICE_MEMORY_TODO_DUE_SOON_DAYS", "5")
    assert reasons(db_path) == {"5日後": "due_soon"}
    monkeypatch.setenv("VOICE_MEMORY_TODO_DUE_SOON_DAYS", "abc")  # bad value falls back to the default
    assert reasons(db_path) == {}


def test_section_lists_title_due_status_and_sources(db_path):
    m = save(db_path, title="元の話")
    make(db_path, "見積もり", "2026-11-08", memory_ids=[m["id"]])
    make(db_path, "いつか", None, days_old=20, due_text="そのうち")
    text = followup.format_section(followup.pending_notices(db_path, NOW), NOW)
    assert "TODO ID:" in text and "見積もり" in text and "2026-11-08(2日超過)" in text
    assert "理由: 期限超過" in text and "[open]" in text and f"関連記録: {m['id']}" in text and "元の話" in text
    assert "日付未確定 本人の表現「そのうち」" in text
    assert followup.format_section([], NOW) == ""


# --- through suggest.run ---------------------------------------------------------------------------------

def stream(send="ok", result="RESULT: sent"):
    import json
    ev = []
    if send:
        ev += [{"type": "assistant", "message": {"content": [
                   {"type": "tool_use", "id": "t2", "name": "mcp__claude_ai_Gmail__send_message", "input": {}}]}},
               {"type": "user", "message": {"content": [
                   {"type": "tool_result", "tool_use_id": "t2", "content": "x", "is_error": send == "error"}]}}]
    ev.append({"type": "result", "is_error": False, "result": result})
    return "\n".join(json.dumps(e) for e in ev)


def runner(out):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=out, stderr="")
    run.calls = calls
    return run


def overdue_todo(db_path):
    d = (datetime.now(timezone.utc) - timedelta(days=3)).astimezone(todos.JST).date().isoformat()
    return todos.add_todo("期限切れ作業", due_date=d, db_path=db_path)


def test_reminders_alone_trigger_a_mail_and_are_recorded_after_send(db_path):
    t = overdue_todo(db_path)
    r = runner(stream())
    suggest.run(runner=r, db_path=db_path)
    assert len(r.calls) == 1 and t["id"] in r.calls[0][2] and "期限超過" in r.calls[0][2]
    r2 = runner(stream())
    suggest.run(runner=r2, db_path=db_path)  # same day again: nothing new
    assert r2.calls == []


@pytest.mark.parametrize("send,result", [("error", "RESULT: sent"), (None, "RESULT: sent")])
def test_failed_send_keeps_reminder_retryable(db_path, send, result):
    overdue_todo(db_path)
    with pytest.raises(suggest.SuggestError):
        suggest.run(runner=runner(stream(send, result)), db_path=db_path)
    assert len(followup.pending_notices(db_path)) == 1
    r = runner(stream())
    suggest.run(runner=r, db_path=db_path)
    assert len(r.calls) == 1


def test_memos_and_reminders_share_one_mail(db_path):
    save(db_path, recorded_at=_days_ago(1))
    overdue_todo(db_path)
    r = runner(stream())
    suggest.run(runner=r, db_path=db_path)
    assert len(r.calls) == 1 and "■ メモID" in r.calls[0][2] and "■■ 採用済みTODOの確認" in r.calls[0][2]


def test_dry_run_shows_reminders_without_recording(db_path, capsys):
    overdue_todo(db_path)
    r = runner(stream())
    suggest.run(dry_run=True, runner=r, db_path=db_path)
    assert "期限切れ作業" in capsys.readouterr().out and r.calls == []
    assert len(followup.pending_notices(db_path)) == 1


def test_all_dropped_by_claude_still_records(db_path):
    overdue_todo(db_path)
    suggest.run(runner=runner(stream(send=None, result="RESULT: none")), db_path=db_path)
    assert followup.pending_notices(db_path) == []


def test_deleting_todo_cascades_notifications(db_path):
    t = overdue_todo(db_path)
    followup.record(followup.pending_notices(db_path), db_path)
    with closing(db.connect(db_path)) as c, c:
        c.execute("DELETE FROM todos WHERE id = ?", (t["id"],))
        assert c.execute("SELECT COUNT(*) FROM todo_notifications").fetchone()[0] == 0
