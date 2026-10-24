import json
import math
import sqlite3
import struct
import wave
from pathlib import Path

import anyio
import pytest

from test_mcp_server import data, params
from mcp import Client

from voice_memory import database as db
from voice_memory.ingest import pipeline
from voice_memory.ingest.audio import InvalidAudioError
from voice_memory.ingest.pipeline import IngestError, process_file
from voice_memory.ingest.summarize import Summary, SummarizeError, parse_summary
from voice_memory.ingest.transcribe import TranscribeError


def make_wav(path: Path, seconds=1.0, freq=440):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / 16000)))
                               for i in range(int(16000 * seconds))))
    return path


class FakeSTT:
    def __init__(self, text="Firebaseの認証方式について話した。Googleログインを採用する。", fail=None):
        self.text, self.fail, self.calls = text, fail, 0

    def transcribe(self, path):
        self.calls += 1
        if self.fail:
            raise self.fail
        return self.text


class FakeLLM:
    def __init__(self, fail=None, tags=("firebase", "認証")):
        self.fail, self.tags, self.calls, self.seen_tags = fail, tags, 0, None

    def summarize(self, transcript, existing_tags):
        self.calls += 1
        self.seen_tags = existing_tags
        if self.fail:
            raise self.fail
        return Summary("認証方式の決定", "決定事項: Googleログイン", list(self.tags))


@pytest.fixture
def env(tmp_path, db_path):
    return dict(db_path=db_path, recordings=tmp_path / "rec"), make_wav(tmp_path / "a.wav")


def run(f, env_, stt=None, llm=None):
    kw, _ = env_
    return process_file(f, stt or FakeSTT(), llm, **kw)


def ingest_row(db_path):
    c = db.connect(db_path)
    return dict(c.execute("SELECT * FROM ingested_files").fetchone())


def test_happy_path_saves_everything(env):
    kw, f = env
    db.save_memory("既存", "x", tags=["firebase"], db_path=kw["db_path"])
    llm = FakeLLM()
    r = run(f, env, llm=llm)
    assert r["status"] == "done"
    m = db.get_memory(r["memory_id"], kw["db_path"])
    assert m["transcript"].startswith("Firebase")
    assert m["summary"] == "決定事項: Googleログイン"
    assert m["tags"] == ["firebase", "認証"]
    assert m["source"] == "audio-ingest"
    assert (kw["recordings"] / m["audio_path"]).is_file()
    assert "firebase" in llm.seen_tags  # existing tags offered for reuse
    assert {t["name"]: t["count"] for t in db.list_tags(kw["db_path"])}["firebase"] == 2
    assert f.exists()


def test_found_via_mcp_tools(env):
    kw, f = env
    r = run(f, env, llm=FakeLLM())

    async def go():
        async with Client(params(kw["db_path"])) as c:
            s = data(await c.call_tool("search_voice_memories", {"query": "Googleログイン"}))
            assert s["count"] == 1 and s["results"][0]["id"] == r["memory_id"]
            g = data(await c.call_tool("get_voice_memory", {"memory_id": r["memory_id"]}))
            assert g["memory"]["audio_path"] == r["audio_path"]
    anyio.run(go)


def test_ingest_into_db_without_fts_indexes_old_and_new(env):
    kw, f = env
    db.save_memory("既存の記録", "旧データの本文", db_path=kw["db_path"])
    with sqlite3.connect(kw["db_path"]) as c:
        c.execute("DROP TABLE memories_fts")  # DB created before FTS; ingest runs before init
    r = run(f, env, llm=FakeLLM())
    assert r["status"] == "done"
    assert [x["title"] for x in db.search_memories("旧データ", db_path=kw["db_path"])] == ["既存の記録"]
    assert [x["id"] for x in db.search_memories("Googleログイン", db_path=kw["db_path"])] == [r["memory_id"]]
    with sqlite3.connect(kw["db_path"]) as c:
        assert c.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0] == 2


def test_duplicate_is_skipped(env):
    kw, f = env
    stt = FakeSTT()
    first = run(f, env, stt, FakeLLM())
    copy = f.with_name("renamed.wav"); copy.write_bytes(f.read_bytes())
    second = run(copy, env, stt, FakeLLM())
    assert second["status"] == "skipped" and second["memory_id"] == first["memory_id"]
    assert stt.calls == 1
    assert len(db.recent_memories(db_path=kw["db_path"])) == 1


def test_stt_failure_keeps_file_and_retry_works(env):
    kw, f = env
    with pytest.raises(IngestError):
        run(f, env, FakeSTT(fail=TranscribeError("boom")), FakeLLM())
    assert f.exists()
    assert list(kw["recordings"].rglob("*.wav"))  # stored copy kept
    assert ingest_row(kw["db_path"])["status"] == "failed"
    assert db.recent_memories(db_path=kw["db_path"]) == []
    assert run(f, env, FakeSTT(), FakeLLM())["status"] == "done"


def test_summary_failure_resumes_without_retranscribing(env):
    kw, f = env
    stt = FakeSTT()
    with pytest.raises(IngestError):
        run(f, env, stt, FakeLLM(fail=SummarizeError("rate limit")))
    assert ingest_row(kw["db_path"])["status"] == "transcribed"
    assert db.recent_memories(db_path=kw["db_path"]) == []
    assert run(f, env, stt, FakeLLM())["status"] == "done"
    assert stt.calls == 1


def test_no_summarizer_stores_transcript_only(env):
    kw, f = env
    r = run(f, env, llm=None)
    m = db.get_memory(r["memory_id"], kw["db_path"])
    assert m["summary"] == "" and m["tags"] == [] and m["transcript"]
    assert m["title"].startswith("a ")


def test_empty_transcript_is_failure(env):
    kw, f = env
    with pytest.raises(IngestError, match="empty"):
        run(f, env, FakeSTT(text="  "), FakeLLM())
    assert ingest_row(kw["db_path"])["status"] == "failed"


def test_db_error_rolls_back_atomically(env, monkeypatch):
    kw, f = env
    orig = db.save_memory

    def broken(*a, conn=None, **k):
        orig(*a, conn=conn, **k)  # insert succeeds inside the transaction...
        raise sqlite3.OperationalError("disk full")  # ...then the transaction fails
    monkeypatch.setattr(pipeline.db, "save_memory", broken)
    with pytest.raises(IngestError):
        run(f, env, FakeSTT(), FakeLLM())
    assert db.recent_memories(db_path=kw["db_path"]) == []  # rolled back
    assert ingest_row(kw["db_path"])["status"] == "transcribed"
    monkeypatch.undo()
    assert run(f, env, FakeSTT(), FakeLLM())["status"] == "done"


def test_empty_and_unsupported_files(env, tmp_path):
    kw, _ = env
    empty = tmp_path / "e.wav"; empty.write_bytes(b"")
    junk = tmp_path / "j.wav"; junk.write_bytes(b"not audio at all" * 100)
    for bad in (empty, junk, tmp_path / "missing.wav"):
        with pytest.raises(InvalidAudioError):
            run(bad, env)
    assert not kw["recordings"].exists() or not list(kw["recordings"].rglob("*.*"))


def test_stale_processing_is_taken_over_fresh_is_skipped(env):
    kw, f = env
    with pytest.raises(IngestError):
        run(f, env, FakeSTT(fail=TranscribeError("x")), FakeLLM())
    c = db.connect(kw["db_path"])
    c.execute("UPDATE ingested_files SET status='processing'"); c.commit()
    assert run(f, env, FakeSTT(), FakeLLM())["status"] == "skipped"
    c.execute("UPDATE ingested_files SET updated_at='2000-01-01T00:00:00+00:00'"); c.commit()
    assert run(f, env, FakeSTT(), FakeLLM())["status"] == "done"


def test_parse_summary_tolerates_bad_tags_and_fences():
    s = parse_summary('```json\n{"title":"t","summary":"s","tags":"oops"}\n```')
    assert (s.title, s.summary, s.tags) == ("t", "s", [])
    s = parse_summary('{"title":"t","summary":"s","tags":["a","a"," b ","c","d","e","f"]}')
    assert s.tags == ["a", "b", "c", "d", "e"]
    with pytest.raises(SummarizeError):
        parse_summary("no json")


def test_api_key_never_logged(env, monkeypatch, caplog):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRET")
    kw, f = env
    with pytest.raises(IngestError):
        run(f, env, FakeSTT(fail=TranscribeError("boom")), FakeLLM())
    assert "SECRET" not in caplog.text


# --- clip_sync ---
def _fake_clip(tmp_path, clip_state="IDLE", sessions=("20261024115101",), fetch_ok=True, charging=False):
    import json
    import subprocess
    from voice_memory.ingest import clip_sync

    calls = []
    box = {"state": clip_state}

    def runner(cmd, timeout):
        calls.append(cmd)
        name = cmd[0]
        if cmd[1].endswith("clip_control.py"):
            if box["state"] is None:
                return subprocess.CompletedProcess(cmd, 1, "", "ConnectionError")
            if cmd[3] == "stop" and box["state"] == "RECORDING":
                box["state"] = "IDLE"
            if cmd[3] == "start" and box["state"] == "IDLE":
                box["state"] = "RECORDING"
            out = json.dumps({"state": box["state"], "charging": charging, "battery": 72})
            return subprocess.CompletedProcess(cmd, 0, out, "")
        if name.endswith("clip_wifi_sync.sh"):
            if fetch_ok:
                d = tmp_path / "recordings" / "Clip" / cmd[1]
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{cmd[1]}.ogg").write_bytes(b"x")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        out = f"Sessions: {len(sessions)}\n" + "".join(f"  {s}: 1 files, 155.5 KB\n" for s in sessions)
        return subprocess.CompletedProcess(cmd, 0, out, "")

    return clip_sync.Clip(runner=runner, sdk=tmp_path), calls


def test_clip_sync_ingests_new_session_once(tmp_path):
    from voice_memory.ingest import clip_sync

    clip, calls = _fake_clip(tmp_path)
    state, ingested = {}, []
    ingest = lambda p: ingested.append(p) or {"memory_id": "m1"}
    r = clip_sync.sync(clip, state, ingest, Path("clip_wifi_sync.sh"))
    assert r["done"] == ["20261024115101"] and len(ingested) == 1
    assert state["20261024115101"]["status"] == "done"
    n = len(calls)
    r = clip_sync.sync(clip, state, ingest, Path("clip_wifi_sync.sh"))  # second run: nothing new
    assert r["new"] == [] and len(ingested) == 1
    assert not any(c[0].endswith("clip_wifi_sync.sh") for c in calls[n:])


def test_clip_sync_quiet_when_out_of_range_or_recording(tmp_path):
    from voice_memory.ingest import clip_sync

    for st in (None, "RECORDING"):
        clip, calls = _fake_clip(tmp_path, clip_state=st, charging=True)
        r = clip_sync.sync(clip, {}, lambda p: {}, Path("s.sh"), follow_charger=False)
        assert r["new"] == [] and not any(c[0].endswith("s.sh") for c in calls)
    for st in (None, "WIFI_SYNC"):
        clip, calls = _fake_clip(tmp_path, clip_state=st)
        r = clip_sync.sync(clip, {}, lambda p: {}, Path("s.sh"))
        assert r["new"] == [] and not any(c[0].endswith("s.sh") for c in calls)


def test_clip_sync_ignored_and_retry_limit(tmp_path):
    from voice_memory.ingest import clip_sync

    clip, _ = _fake_clip(tmp_path, sessions=("20261024032642", "20261024115101"), fetch_ok=False)
    state = {"20261024032642": {"status": "ignored"}}
    for _ in range(clip_sync.MAX_ATTEMPTS + 2):
        r = clip_sync.sync(clip, state, lambda p: {}, Path("s.sh"), follow_charger=False)
    assert state["20261024115101"]["attempts"] == clip_sync.MAX_ATTEMPTS
    assert state["20261024115101"]["status"] == "failed" and r["new"] == []
    assert "20261024032642" not in r["new"]


def test_clip_sync_dry_run_changes_nothing(tmp_path):
    from voice_memory.ingest import clip_sync

    clip, calls = _fake_clip(tmp_path)
    state = {}
    r = clip_sync.sync(clip, state, lambda p: {}, Path("s.sh"), dry_run=True)
    assert r["new"] == ["20261024115101"] and state == {}
    assert not any(c[0].endswith("s.sh") for c in calls)


def test_clip_sync_stops_recording_on_charger_then_fetches(tmp_path):
    from voice_memory.ingest import clip_sync

    clip, calls = _fake_clip(tmp_path, clip_state="RECORDING", charging=True)
    r = clip_sync.sync(clip, {}, lambda p: {"memory_id": "m1"}, Path("clip_wifi_sync.sh"))
    assert r["stopped"] is True and r["done"] == ["20261024115101"]
    assert [c[3] for c in calls if c[1].endswith("clip_control.py")] == ["state", "stop"]  # still charging: no restart


def test_clip_sync_resumes_recording_off_charger(tmp_path):
    from voice_memory.ingest import clip_sync

    clip, calls = _fake_clip(tmp_path, clip_state="IDLE", charging=False)
    r = clip_sync.sync(clip, {}, lambda p: {"memory_id": "m1"}, Path("clip_wifi_sync.sh"))
    assert r["resumed"] is True
    assert [c[3] for c in calls if c[1].endswith("clip_control.py")] == ["state", "start"]
    clip, calls = _fake_clip(tmp_path, clip_state="IDLE", charging=True)  # on the charger: left idle
    r = clip_sync.sync(clip, {}, lambda p: {}, Path("clip_wifi_sync.sh"))
    assert "resumed" not in r and not any(c[3] == "start" for c in calls if c[1].endswith("clip_control.py"))


def test_clip_fetch_nonzero_exit_and_stale_ogg_are_failures(tmp_path):
    import subprocess
    from voice_memory.ingest import clip_sync

    s = "20261024115101"
    d = tmp_path / "recordings" / "Clip" / s
    d.mkdir(parents=True)
    (d / f"{s}.ogg").write_bytes(b"partial")  # left behind by an earlier, interrupted attempt

    seen = {}

    def runner(cmd, timeout):
        seen["existed_at_start"] = (d / f"{s}.ogg").exists()
        return subprocess.CompletedProcess(cmd, 124, "timeout", "")

    clip = clip_sync.Clip(runner=runner, sdk=tmp_path)
    assert clip.fetch(s, Path("x.sh")) is None  # script failed: even a written file is not trusted
    assert seen["existed_at_start"] is False  # stale file was removed before the fetch

    def writes_then_fails(cmd, timeout):
        (d / f"{s}.ogg").write_bytes(b"cut off")
        return subprocess.CompletedProcess(cmd, 124, "", "")

    assert clip_sync.Clip(runner=writes_then_fails, sdk=tmp_path).fetch(s, Path("x.sh")) is None


def test_clip_sync_failed_fetch_still_resumes_recording(tmp_path):
    from voice_memory.ingest import clip_sync

    clip, calls = _fake_clip(tmp_path, clip_state="IDLE", charging=False, fetch_ok=False)
    state = {}
    r = clip_sync.sync(clip, state, lambda p: {}, Path("clip_wifi_sync.sh"))
    assert r["failed"] == ["20261024115101"] and r["resumed"] is True
    assert state["20261024115101"]["status"] == "failed"
