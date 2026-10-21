import sqlite3
import pytest
from voice_memory import database as db


def save(db_path, **kw):
    args = dict(title="Firebase認証方式の検討", transcript="メール認証とGoogleログインを比較した。",
                summary="認証方式の比較", tags=["firebase", "認証"], db_path=db_path)
    args.update(kw)
    return db.save_memory(**args)


def test_init_and_wal(db_path):
    conn = db.connect(db_path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"memories", "tags", "memory_tags"} <= names


def test_init_idempotent(db_path):
    save(db_path)
    db.init_db(db_path); db.init_db(db_path)
    assert len(db.recent_memories(db_path=db_path)) == 1


def test_save_and_get(db_path):
    r = save(db_path)
    m = db.get_memory(r["id"], db_path)
    assert m["title"] == "Firebase認証方式の検討"
    assert m["tags"] == ["firebase", "認証"]
    assert m["audio_path"] is None and m["source"] == "manual"


def test_search_japanese(db_path):
    save(db_path)
    save(db_path, title="買い物", transcript="牛乳を買う", summary="", tags=["生活"])
    res = db.search_memories("認証", db_path=db_path)
    assert [x["title"] for x in res] == ["Firebase認証方式の検討"]
    assert db.search_memories("牛乳", db_path=db_path)[0]["title"] == "買い物"
    assert "Googleログイン" in db.search_memories("Googleログイン", db_path=db_path)[0]["excerpt"]


def test_search_by_tag(db_path):
    save(db_path, transcript="本文に語なし", title="t", summary="", tags=["tagonly"])
    assert len(db.search_memories("tagonly", db_path=db_path)) == 1


def test_search_wildcards_are_literal(db_path):
    save(db_path)
    assert db.search_memories("%", db_path=db_path) == []
    assert db.search_memories("_", db_path=db_path) == []


def test_sql_injection_is_inert(db_path):
    save(db_path)
    assert db.search_memories("'; DROP TABLE memories; --", db_path=db_path) == []
    assert len(db.recent_memories(db_path=db_path)) == 1


def test_get_missing(db_path):
    with pytest.raises(db.NotFoundError):
        db.get_memory("00000000-0000-0000-0000-000000000000", db_path)


def test_limit_cap(db_path):
    for i in range(60):
        save(db_path, title=f"n{i}", tags=[])
    assert len(db.recent_memories(1000, db_path)) == db.MAX_LIMIT
    assert len(db.search_memories("n", 1000, db_path)) == db.MAX_LIMIT
    assert len(db.recent_memories(3, db_path)) == 3


@pytest.mark.parametrize("kw", [
    {"title": ""}, {"title": "   "}, {"transcript": ""}, {"title": "x" * 201},
    {"summary": "x" * 2001}, {"transcript": "x" * 100_001},
    {"tags": ["a"] * 21 and [str(i) for i in range(21)]}, {"tags": ["x" * 51]}, {"tags": [""]},
    {"audio_path": "/etc/passwd"}, {"audio_path": "../x.wav"},
    {"recorded_at": "yesterday"}, {"title": "a\x00b"},
])
def test_invalid_input(db_path, kw):
    with pytest.raises(db.ValidationError):
        save(db_path, **kw)
    assert db.recent_memories(db_path=db_path) == []


@pytest.mark.parametrize("limit", [0, -1, "5", True])
def test_invalid_limit(db_path, limit):
    with pytest.raises(db.ValidationError):
        db.recent_memories(limit, db_path)


def test_empty_query_rejected(db_path):
    with pytest.raises(db.ValidationError):
        db.search_memories("  ", db_path=db_path)


def test_tag_counts_and_dedup(db_path):
    save(db_path, tags=["a", "a", "b"])
    save(db_path, tags=["a"])
    assert db.list_tags(db_path) == [{"name": "a", "count": 2}, {"name": "b", "count": 1}]


def test_foreign_key_enforced(db_path):
    conn = db.connect(db_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO memory_tags (memory_id, tag_id) VALUES ('nope', 1)")


def test_recent_order(db_path):
    save(db_path, title="old", recorded_at="2026-01-01T00:00:00")
    save(db_path, title="new", recorded_at="2026-02-01T00:00:00")
    assert [m["title"] for m in db.recent_memories(db_path=db_path)] == ["new", "old"]


def test_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICE_MEMORY_DB", str(tmp_path / "x" / "e.db"))
    db.init_db()
    assert (tmp_path / "x" / "e.db").exists()
