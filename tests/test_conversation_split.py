import sqlite3
from contextlib import closing

import pytest

from test_ingest import FakeLLM, make_wav
from voice_memory import database as db, digest
from voice_memory.ingest import pipeline
from voice_memory.ingest.pipeline import IngestError, process_file
from voice_memory.ingest.split import METHOD, SplitConfig
from voice_memory.ingest.summarize import Summary
from voice_memory.ingest.transcribe import TimedTranscript

LONG = SplitConfig(min_duration=1.0)  # the test wav is 1 s long


def talk(start, end, tag):
    segs, t = [], start
    while t < end:
        segs.append((t, min(t + 10, end), (tag + str(int(t))).ljust(60, "あ")))
        t += 10
    return segs, [(start, end)]


def timed_two_blocks():
    a, sa = talk(0, 300, "a")
    b, sb = talk(500, 900, "b")
    return TimedTranscript(a + b, sa + sb)


class TimedSTT:
    def __init__(self, timed):
        self.timed, self.calls = timed, 0

    def transcribe_timed(self, path):
        self.calls += 1
        return self.timed

    def transcribe(self, path):
        return self.transcribe_timed(path).text


class NumberedLLM:
    """Summaries differ per chunk; optionally fails on the n-th call."""

    def __init__(self, fail_on=None):
        self.calls, self.fail_on = 0, fail_on

    def summarize(self, transcript, existing_tags):
        self.calls += 1
        if self.fail_on == self.calls:
            from voice_memory.ingest.summarize import SummarizeError
            raise SummarizeError("boom")
        return Summary(f"title {transcript[:2]}", f"summary {transcript[:2]}", ["t" + transcript[:1]])


@pytest.fixture
def env(tmp_path, db_path):
    return dict(db_path=db_path, recordings=tmp_path / "rec", split_config=LONG), make_wav(tmp_path / "20261101 100000-AAAA.wav")


def mem_rows(db_path):
    with closing(db.connect(db_path)) as c:
        return [dict(r) for r in c.execute("SELECT * FROM memories ORDER BY audio_start_sec")]


def test_long_file_becomes_several_memories_in_one_transaction(env):
    kw, f = env
    r = process_file(f, TimedSTT(timed_two_blocks()), NumberedLLM(), **kw)
    assert r["status"] == "done" and r["chunks"] == 2 and len(r["memory_ids"]) == 2
    rows = mem_rows(kw["db_path"])
    assert [(m["audio_start_sec"], m["audio_end_sec"]) for m in rows] == [(0, 300), (500, 900)]
    assert {m["split_method"] for m in rows} == {METHOD} and {m["archived"] for m in rows} == {0}
    assert len({m["audio_path"] for m in rows}) == 1
    assert rows[0]["recorded_at"] < rows[1]["recorded_at"]  # Voice Memos name: start + offset
    with closing(db.connect(kw["db_path"])) as c:
        links = c.execute("SELECT segment_index FROM ingested_file_memories ORDER BY segment_index").fetchall()
        assert [x[0] for x in links] == [0, 1]
        f_row = c.execute("SELECT status, memory_id FROM ingested_files").fetchone()
        assert f_row["status"] == "done" and f_row["memory_id"] == r["memory_id"]
        assert c.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0] == 2
    # chunk texts together are the whole transcript
    assert "\n".join(m["transcript"] for m in rows) == timed_two_blocks().text


def test_failure_on_second_chunk_leaves_nothing_then_retry_completes(env):
    kw, f = env
    stt = TimedSTT(timed_two_blocks())
    with pytest.raises(IngestError):
        process_file(f, stt, NumberedLLM(fail_on=2), **kw)
    with closing(db.connect(kw["db_path"])) as c:
        assert c.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM ingested_file_memories").fetchone()[0] == 0
        row = c.execute("SELECT status, transcript, segments_json FROM ingested_files").fetchone()
        assert row["status"] == "transcribed" and row["transcript"] and row["segments_json"]
    r = process_file(f, stt, NumberedLLM(), **kw)  # resumes from the cached timed transcript
    assert r["status"] == "done" and r["chunks"] == 2 and stt.calls == 1
    assert process_file(f, stt, NumberedLLM(), **kw)["status"] == "skipped"
    assert len(mem_rows(kw["db_path"])) == 2


def test_short_file_and_disabled_split_stay_one_memory(env):
    kw, f = env
    r = process_file(f, TimedSTT(timed_two_blocks()), NumberedLLM(), **{**kw, "split_config": SplitConfig(min_duration=600)})
    assert r["chunks"] == 1
    m = mem_rows(kw["db_path"])[0]
    assert m["audio_start_sec"] is None and m["split_method"] is None


def test_split_disabled_by_config(env, tmp_path):
    kw, f = env
    r = process_file(f, TimedSTT(timed_two_blocks()), NumberedLLM(),
                     **{**kw, "split_config": SplitConfig(enabled=False, min_duration=1)})
    assert r["chunks"] == 1


def test_single_chunk_result_is_not_marked_as_split(env):
    kw, f = env
    a, sa = talk(0, 300, "a")
    r = process_file(f, TimedSTT(TimedTranscript(a, sa)), NumberedLLM(), **kw)
    assert r["chunks"] == 1 and mem_rows(kw["db_path"])[0]["split_method"] is None


def test_fake_transcriber_without_timing_still_works(env):
    kw, f = env
    from test_ingest import FakeSTT
    r = process_file(f, FakeSTT(), FakeLLM(), **kw)
    assert r["status"] == "done" and r["chunks"] == 1


def test_archived_memories_are_hidden_from_every_listing(env):
    kw, _ = env
    p = kw["db_path"]
    a = db.save_memory("parent", "keyword alpha", tags=["tg"], db_path=p)
    b = db.save_memory("child", "keyword beta", tags=["tg"], db_path=p)
    with closing(db.connect(p)) as c, c:
        c.execute("UPDATE memories SET archived = 1 WHERE id = ?", (a["id"],))
    assert [m["id"] for m in db.search_memories("keyword", db_path=p)] == [b["id"]]
    assert [m["id"] for m in db.search_memories("keyword", db_path=p, include_archived=True)].count(a["id"]) == 1
    assert [m["id"] for m in db.recent_memories(db_path=p)] == [b["id"]]
    assert [m["id"] for m in db.memories_by_tag("tg", db_path=p)] == [b["id"]]
    assert [m["id"] for m in db.pending_memories(db_path=p)] == [b["id"]]
    assert db.list_tags(p) == [{"name": "tg", "count": 1}]
    assert db.get_memory(a["id"], p)["archived"] == 1  # still readable by id


def test_migration_adds_columns_to_old_db_idempotently(tmp_path):
    p = tmp_path / "old.db"
    old_memories = """CREATE TABLE memories (
        id TEXT PRIMARY KEY, recorded_at TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
        transcript TEXT NOT NULL, audio_path TEXT, source TEXT NOT NULL DEFAULT 'manual', created_at TEXT NOT NULL);"""
    with closing(sqlite3.connect(p)) as c:
        c.executescript(old_memories)
        c.execute("INSERT INTO memories VALUES ('m1','2026-01-01T00:00:00+00:00','t','s','x','a/b.m4a','audio-ingest','c')")
        c.commit()
    db.init_db(p)
    db.init_db(p)
    with closing(db.connect(p)) as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(memories)")}
        assert {"audio_start_sec", "audio_end_sec", "parent_memory_id", "split_method", "archived"} <= cols
        row = c.execute("SELECT * FROM memories").fetchone()
        assert row["id"] == "m1" and row["archived"] == 0 and row["audio_start_sec"] is None
        assert c.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0] == 1
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='ingested_file_memories'").fetchone()
    assert db.migrate(db.connect(p)) == []


def test_old_ingested_files_table_gains_segments_column(env, tmp_path):
    kw, f = env
    with closing(sqlite3.connect(kw["db_path"])) as c:
        c.executescript(pipeline.SCHEMA.replace(",\n    segments_json TEXT", ""))
        assert "segments_json" not in {r[1] for r in c.execute("PRAGMA table_info(ingested_files)")}
    r = process_file(f, TimedSTT(timed_two_blocks()), NumberedLLM(), **kw)
    assert r["chunks"] == 2


def test_deleting_one_chunk_keeps_file_ingested_until_the_last_is_gone(env):
    kw, f = env
    r = process_file(f, TimedSTT(timed_two_blocks()), NumberedLLM(), **kw)
    m1, m2 = r["memory_ids"]
    db.delete_memory(m1, recordings=kw["recordings"], db_path=kw["db_path"])
    with closing(db.connect(kw["db_path"])) as c:
        row = c.execute("SELECT status, memory_id FROM ingested_files").fetchone()
        assert row["status"] == "done" and row["memory_id"] == m2
    assert (kw["recordings"] / db.get_memory(m2, kw["db_path"])["audio_path"]).is_file()  # audio shared, kept
    assert process_file(f, TimedSTT(timed_two_blocks()), NumberedLLM(), **kw)["status"] == "skipped"
    out = db.delete_memory(m2, recordings=kw["recordings"], db_path=kw["db_path"])
    assert out["audio_deleted"]
    with closing(db.connect(kw["db_path"])) as c:
        assert c.execute("SELECT status FROM ingested_files").fetchone()["status"] == "deleted"
    assert process_file(f, TimedSTT(timed_two_blocks()), NumberedLLM(), **kw)["status"] == "skipped"


def test_deleting_a_parent_detaches_its_children(db_path):
    parent = db.save_memory("p", "x", db_path=db_path)
    child = db.save_memory("c", "y", parent_memory_id=parent["id"], db_path=db_path)
    db.delete_memory(parent["id"], db_path=db_path)
    assert db.get_memory(child["id"], db_path)["parent_memory_id"] is None


def test_mail_groups_memos_from_one_recording(env):
    kw, f = env
    r = process_file(f, TimedSTT(timed_two_blocks()), NumberedLLM(), **kw)
    # The fixture is named 2026-11-01, so as calendar time passes its memos fall
    # outside digest's 7-day pending window. Shift them to "now" (keeping their
    # spacing) so this test checks grouping, not the date it happens to run on.
    from datetime import datetime, timezone
    rows = mem_rows(kw["db_path"])
    shift = datetime.now(timezone.utc) - datetime.fromisoformat(rows[0]["recorded_at"])
    with closing(db.connect(kw["db_path"])) as c:
        for m in rows:
            c.execute("UPDATE memories SET recorded_at = ? WHERE id = ?",
                      ((datetime.fromisoformat(m["recorded_at"]) + shift).isoformat(timespec="seconds"), m["id"]))
        c.commit()
    db.save_memory("other", "別の短いメモ", db_path=kw["db_path"])
    body, ids = digest.build(db_path=kw["db_path"])
    assert body.count("同じ録音ファイルから会話ごとに分割されたメモ(2件") == 1
    assert set(r["memory_ids"]) <= set(ids) and len(ids) == 3
    first, second = (body.index(i) for i in r["memory_ids"])
    assert first < second and "録音ファイル内 0:00:00〜0:05:00" in body
