import sqlite3
from contextlib import closing
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


# ---- FTS5 (trigram) -------------------------------------------------------

def fts_rows(db_path):
    with sqlite3.connect(db_path) as c:
        return sorted(c.execute("SELECT memory_id, title, summary, transcript, tags FROM memories_fts"))


def test_fts_long_term_substring(db_path):
    save(db_path)
    assert len(db.search_memories("Googleログイン", db_path=db_path)) == 1
    assert len(db.search_memories("ログイン", db_path=db_path)) == 1  # 3 chars, mid-word
    assert db.search_memories("googleログ", db_path=db_path)  # case-insensitive


def test_search_two_char_term_uses_like(db_path):
    save(db_path)
    assert len(db.search_memories("認証", db_path=db_path)) == 1
    assert db.search_memories("ログ", db_path=db_path)  # 2 chars, mid-word
    assert db.search_memories("認", db_path=db_path)


def test_search_and_mixed_terms(db_path):
    save(db_path)
    save(db_path, title="別件", transcript="Googleカレンダーの話", summary="", tags=[])
    both = db.search_memories("Google 認証", db_path=db_path)
    assert [x["title"] for x in both] == ["Firebase認証方式の検討"]
    assert len(db.search_memories("Google", db_path=db_path)) == 2
    assert db.search_memories("Google 牛乳", db_path=db_path) == []
    assert db.search_memories("Firebase Google 認証", db_path=db_path)  # all long + short


def test_search_short_term_wildcards_still_literal_with_long_term(db_path):
    save(db_path)
    assert db.search_memories("Google %", db_path=db_path) == []
    save(db_path, title="割引", transcript="50% off セール", summary="", tags=[])
    assert [x["title"] for x in db.search_memories("50% off", db_path=db_path)] == ["割引"]


@pytest.mark.parametrize("q", ['"', 'a"b"c', "*", "NEAR(a b)", "AND", "OR NOT", "title:認証", "foo*", '"unterminated',
                               "(((", "memory_id:x", "^abc", "-abc", "abc AND def"])
def test_fts_syntax_chars_are_inert(db_path, q):
    save(db_path)
    assert db.search_memories(q, db_path=db_path) == []  # no exception, no match


def test_fts_special_chars_match_literally(db_path):
    save(db_path, title="記法", transcript='式は a AND b と "引用" と foo* を含む', summary="", tags=[])
    for q, hit in [("foo*", True), ('"引用"', True), ("a AND b", True), ("NEAR", False), ("foo*bar", False)]:
        assert bool(db.search_memories(q, db_path=db_path)) is hit, q


def test_query_whitespace_only(db_path):
    for q in ["", "   ", "\n\t"]:
        with pytest.raises(db.ValidationError):
            db.search_memories(q, db_path=db_path)


def test_fts_tag_only_long_term(db_path):
    save(db_path, transcript="本文", title="t", summary="", tags=["longtagname"])
    assert len(db.search_memories("longtagname", db_path=db_path)) == 1


def test_fts_matches_source_after_save(db_path):
    a = save(db_path)
    b = save(db_path, title="買い物", transcript="牛乳を買う", summary="", tags=["生活", "家"])
    rows = fts_rows(db_path)
    assert len(rows) == 2
    by_id = {r[0]: r for r in rows}
    assert by_id[b["id"]][1:] == ("買い物", "", "牛乳を買う", "生活 家")
    assert by_id[a["id"]][4] == "firebase 認証"


def test_save_with_conn_joins_transaction_rollback(db_path):
    conn = db.connect(db_path)
    with conn:
        db.save_memory("t", "本文テキスト", conn=conn)
    assert len(fts_rows(db_path)) == 1
    try:
        with conn:
            db.save_memory("t2", "ロールバック対象", tags=["x"], conn=conn)
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    conn.close()
    assert len(fts_rows(db_path)) == 1
    assert db.search_memories("ロールバック対象", db_path=db_path) == []
    assert len(db.recent_memories(db_path=db_path)) == 1


def test_save_own_connection_atomic(db_path, monkeypatch):
    """If the FTS insert fails, the memory row must not be committed either."""
    with sqlite3.connect(db_path) as c:
        c.execute("DROP TABLE memories_fts")
    with pytest.raises(sqlite3.OperationalError):
        save(db_path)
    assert db.recent_memories(db_path=db_path) == []


def test_rebuild_fts_idempotent_and_matches(db_path):
    save(db_path); save(db_path, title="買い物", transcript="牛乳を買う", summary="", tags=["生活"])
    before = fts_rows(db_path)
    with closing(db.connect(db_path)) as conn:
        assert db.rebuild_fts(conn) == 2
        assert db.rebuild_fts(conn) == 2
    assert fts_rows(db_path) == before


def test_rebuild_fts_failure_leaves_index_untouched(db_path, monkeypatch):
    save(db_path)
    before = fts_rows(db_path)
    conn = db.connect(db_path)
    real = db._fts_count
    monkeypatch.setattr(db, "_fts_count", lambda c: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        db.rebuild_fts(conn)
    monkeypatch.setattr(db, "_fts_count", real)
    conn.close()
    assert fts_rows(db_path) == before


def test_init_db_migrates_existing_db_without_fts(tmp_path):
    p = tmp_path / "old.db"
    db.init_db(p)
    ids = [save(p, title=f"記録{i}", transcript=f"本文{i} 検索対象語", tags=["タグ"])["id"] for i in range(3)]
    with sqlite3.connect(p) as c:
        c.execute("DROP TABLE memories_fts")  # simulate a DB created before FTS
    db.init_db(p)
    assert len(fts_rows(p)) == 3
    assert len(db.search_memories("検索対象語", db_path=p)) == 3
    # source data untouched
    assert {r["id"] for r in db.recent_memories(db_path=p)} == set(ids)
    db.init_db(p)
    assert len(fts_rows(p)) == 3  # no duplicates on re-run


def test_init_db_heals_drift(db_path):
    save(db_path)
    with sqlite3.connect(db_path) as c:
        c.execute("DELETE FROM memories_fts")
    db.init_db(db_path)
    assert len(fts_rows(db_path)) == 1


def test_rebuild_fts_refuses_open_transaction_and_keeps_it_pending(db_path):
    save(db_path)
    with db.connect(db_path) as conn:
        conn.execute("UPDATE memories SET title = 'pending-title'")  # opens the caller's transaction
        assert conn.in_transaction
        with pytest.raises(RuntimeError):
            db.rebuild_fts(conn)
        assert conn.in_transaction  # still the caller's, not committed behind its back
        conn.rollback()
    with sqlite3.connect(db_path) as c:
        assert c.execute("SELECT COUNT(*) FROM memories WHERE title = 'pending-title'").fetchone()[0] == 0


def test_ensure_fts_misses_same_count_drift_but_rebuild_fixes_it(db_path):
    save(db_path)
    with sqlite3.connect(db_path) as c:
        c.execute("UPDATE memories_fts SET title = 'stale'")  # same count, wrong content
    db.init_db(db_path)
    assert fts_rows(db_path)[0][1] == "stale"  # documented limitation of the count check
    with closing(db.connect(db_path)) as conn:
        db.rebuild_fts(conn)
    assert fts_rows(db_path)[0][1] != "stale"


def _days_ago(n):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat(timespec="seconds")


def test_pending_window_and_exclusion(db_path):
    new = save(db_path, title="新しい", recorded_at=_days_ago(1))
    save(db_path, title="古い", recorded_at=_days_ago(30))
    assert [m["id"] for m in db.pending_memories(db_path=db_path)] == [new["id"]]
    assert len(db.pending_memories(days=60, db_path=db_path)) == 2
    db.record_registration(new["id"], "event", "evt1", db_path)
    assert db.pending_memories(db_path=db_path) == []


def test_pending_handles_offsets(db_path):
    from datetime import datetime, timedelta, timezone
    jst = timezone(timedelta(hours=9))
    m = save(db_path, recorded_at=(datetime.now(jst) - timedelta(hours=1)).isoformat(timespec="seconds"))
    assert [x["id"] for x in db.pending_memories(days=1, db_path=db_path)] == [m["id"]]


def test_record_registration_rules(db_path):
    m = save(db_path)
    assert db.record_registration(m["id"], "none", db_path=db_path)["external_id"] is None
    assert db.pending_memories(db_path=db_path) == []
    with pytest.raises(db.ValidationError):
        db.record_registration(m["id"], "event", db_path=db_path)  # external_id required
    with pytest.raises(db.ValidationError):
        db.record_registration(m["id"], "none", "x", db_path=db_path)
    with pytest.raises(db.ValidationError):
        db.record_registration(m["id"], "todo", "x", db_path=db_path)
    with pytest.raises(db.NotFoundError):
        db.record_registration("nope", "task", "t1", db_path=db_path)
    with pytest.raises(db.ValidationError):
        db.pending_memories(days=0, db_path=db_path)


def test_registrations_cascade(db_path):
    m = save(db_path)
    db.record_registration(m["id"], "task", "t1", db_path)
    with closing(db.connect(db_path)) as c:
        c.execute("DELETE FROM memories WHERE id = ?", (m["id"],)); c.commit()
        assert c.execute("SELECT COUNT(*) FROM registrations").fetchone()[0] == 0


def test_mark_suggested_hides_from_daily_run_only(db_path):
    a = save(db_path, title="a", recorded_at=_days_ago(1))
    b = save(db_path, title="b", recorded_at=_days_ago(1))
    assert db.mark_suggested([a["id"], "nope"], db_path) == 1
    assert db.mark_suggested([a["id"]], db_path) == 0
    assert [m["id"] for m in db.pending_memories(db_path=db_path, exclude_suggested=True)] == [b["id"]]
    assert len(db.pending_memories(db_path=db_path)) == 2
