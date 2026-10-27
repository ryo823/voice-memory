import sqlite3
import threading
from contextlib import closing
from datetime import date, timedelta

import pytest

from voice_memory import backup, database as db, digest, todos
from voice_memory.database import NotFoundError, ValidationError
from test_database import _days_ago, save

OLD_TABLES = ["memories", "tags", "memory_tags", "registrations", "suggestions"]


def counts(path):
    with closing(sqlite3.connect(path)) as c:
        return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in OLD_TABLES + ["memories_fts"]}


def test_migration_keeps_existing_data(tmp_path):
    p = tmp_path / "old.db"
    old_schema = db.SCHEMA[:db.SCHEMA.index("-- TODOs the user")] + db.SCHEMA[db.SCHEMA.index("-- Keyed by memory_id"):]
    with closing(sqlite3.connect(p)) as c:
        c.executescript(old_schema)
    a = save(p)
    save(p, title="b", tags=["x"])
    db.record_registration(a["id"], "none", db_path=p)
    db.mark_suggested([a["id"]], p)
    before = counts(p)
    with closing(sqlite3.connect(p)) as c:
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name = 'todos'").fetchone()
    db.init_db(p); db.init_db(p)
    assert counts(p) == before
    assert todos.list_todos(db_path=p) == []


def test_add_get_and_source_link(db_path):
    m = save(db_path)
    t = todos.add_todo("見積もりを送る", detail="x社向け", due_date="2026-11-05", priority="high",
                       memory_ids=[m["id"]], db_path=db_path)
    assert (t["status"], t["duplicate"], t["memory_ids"], t["completed_at"]) == ("open", False, [m["id"]], None)
    assert t["created_at"] == t["updated_at"] == t["last_reviewed_at"]
    g = todos.get_todo(t["id"], db_path)
    assert g["memories"][0]["id"] == m["id"]
    assert g["due_date"] == "2026-11-05" and g["priority"] == "high"


def test_todo_without_source_memory_is_fine(db_path):
    t = todos.add_todo("牛乳を買う", db_path=db_path)
    assert t["memory_ids"] == [] and t["due_date"] is None and t["priority"] is None


def test_duplicates_ignore_width_spacing_punctuation_and_merge_links(db_path):
    a, b = save(db_path), save(db_path)
    t1 = todos.add_todo("ＡＢＣ社に 連絡する。", memory_ids=[a["id"]], db_path=db_path)
    t2 = todos.add_todo("abc社に連絡する", memory_ids=[b["id"]], db_path=db_path)
    assert t2["duplicate"] and t2["id"] == t1["id"] and set(t2["memory_ids"]) == {a["id"], b["id"]}
    assert len(todos.list_todos("all", db_path=db_path)) == 1
    t3 = todos.add_todo("abc社に連絡する", db_path=db_path)  # no memory at all: still not duplicated
    assert t3["duplicate"]


def test_closed_todo_does_not_block_a_new_one_and_reopen_conflict(db_path):
    t1 = todos.add_todo("申請", db_path=db_path)
    todos.set_todo_status(t1["id"], "cancelled", db_path)
    t2 = todos.add_todo("申請", db_path=db_path)
    assert not t2["duplicate"] and t2["id"] != t1["id"]
    with pytest.raises(ValidationError):
        todos.set_todo_status(t1["id"], "open", db_path)
    assert todos.get_todo(t1["id"], db_path)["status"] == "cancelled"


def test_status_flow_and_completed_at(db_path):
    t = todos.add_todo("作業", db_path=db_path)
    assert todos.set_todo_status(t["id"], "in_progress", db_path)["completed_at"] is None
    done = todos.set_todo_status(t["id"], "completed", db_path)
    assert done["completed_at"]
    again = todos.set_todo_status(t["id"], "completed", db_path)
    assert again["completed_at"] == done["completed_at"]
    assert todos.set_todo_status(t["id"], "open", db_path)["completed_at"] is None
    for s in ("cancelled", "dismissed"):
        assert todos.set_todo_status(t["id"], s, db_path)["status"] == s
    with pytest.raises(ValidationError):
        todos.set_todo_status(t["id"], "done", db_path)


def test_update_fields_clear_and_unchanged(db_path):
    t = todos.add_todo("作業", due_date="2026-11-05", due_text="来週", priority="low", db_path=db_path)
    u = todos.update_todo(t["id"], detail="詳細", db_path=db_path)
    assert (u["detail"], u["due_date"], u["due_text"], u["priority"]) == ("詳細", "2026-11-05", "来週", "low")
    c = todos.update_todo(t["id"], due_date="", due_text="", priority="", db_path=db_path)
    assert (c["due_date"], c["due_text"], c["priority"]) == (None, None, None)
    r = todos.update_todo(t["id"], reviewed=True, db_path=db_path)
    assert r["last_reviewed_at"] >= t["last_reviewed_at"]
    with pytest.raises(NotFoundError):
        todos.update_todo("nope", title="x", db_path=db_path)


def test_due_date_states_overdue_future_unset_vague(db_path):
    past = (date.today() - timedelta(days=3)).isoformat()
    future = (date.today() + timedelta(days=30)).isoformat()
    todos.add_todo("過去", due_date=past, db_path=db_path)
    todos.add_todo("未来", due_date=future, db_path=db_path)
    todos.add_todo("未設定", db_path=db_path)
    todos.add_todo("曖昧", due_text="そのうち", db_path=db_path)
    by = {t["title"]: t for t in todos.list_todos(db_path=db_path)}
    assert by["過去"]["overdue"] and not by["未来"]["overdue"] and not by["未設定"]["overdue"]
    assert by["曖昧"]["due_date"] is None and by["曖昧"]["due_text"] == "そのうち"
    assert [t["title"] for t in todos.list_todos(db_path=db_path)][:2] == ["過去", "未来"]  # dated first
    assert [t["title"] for t in todos.list_todos(overdue_only=True, db_path=db_path)] == ["過去"]


@pytest.mark.parametrize("kw", [
    {"title": ""}, {"title": "  "}, {"title": "!!!"}, {"title": "x" * 201},
    {"title": "x", "due_date": "来週"}, {"title": "x", "due_date": "2026-02-30"},
    {"title": "x", "due_date": "2026/11/05"}, {"title": "x", "priority": "urgent"},
    {"title": "x", "memory_ids": "abc"}, {"title": "x", "memory_ids": ["a"] * 1 + ["b"] * 0 + [str(i) for i in range(25)]},
    {"title": "x\x00"}, {"title": 5},
])
def test_validation(db_path, kw):
    with pytest.raises(ValidationError):
        todos.add_todo(db_path=db_path, **kw)
    assert todos.list_todos("all", db_path=db_path) == []


def test_unknown_memory_rolls_back_whole_add(db_path):
    with pytest.raises(NotFoundError):
        todos.add_todo("x", memory_ids=["nope"], db_path=db_path)
    assert todos.list_todos("all", db_path=db_path) == []


def test_deleting_memory_keeps_todo(db_path, tmp_path):
    m = save(db_path)
    t = todos.add_todo("残る", memory_ids=[m["id"]], db_path=db_path)
    db.delete_memory(m["id"], recordings=tmp_path, db_path=db_path)
    assert todos.get_todo(t["id"], db_path)["memory_ids"] == []


def test_concurrent_adds_make_one_todo(db_path):
    results, errors = [], []

    def go():
        try:
            results.append(todos.add_todo("同時登録", db_path=db_path))
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errors
    assert len({r["id"] for r in results}) == 1 and sum(not r["duplicate"] for r in results) == 1
    assert len(todos.list_todos("all", db_path=db_path)) == 1


def test_digest_shows_adopted_todo_for_memo_and_related(db_path):
    old = save(db_path, title="過去", tags=["案件Y"], recorded_at=_days_ago(30))
    new = save(db_path, tags=["案件Y"], recorded_at=_days_ago(1))
    todos.add_todo("見積もり提出", due_text="今月中", memory_ids=[old["id"]], db_path=db_path)
    body, _ = digest.build(db_path)
    assert "採用済みの関連TODO" in body and "見積もり提出" in body
    assert "期限: 日付未確定(本人の表現「今月中」)" in body


def test_backup_snapshot_includes_todos(db_path, tmp_path):
    m = save(db_path)
    t = todos.add_todo("バックアップ確認", memory_ids=[m["id"]], db_path=db_path)
    dest = tmp_path / "bk"
    backup.run_backup(db_path, tmp_path / "rec", None, dest)
    assert backup.verify(dest)["memories"] == 1
    restored = sorted(dest.glob("**/voice_memory-*.db"))[-1]
    got = todos.get_todo(t["id"], restored)
    assert got["title"] == "バックアップ確認" and got["memory_ids"] == [m["id"]]
