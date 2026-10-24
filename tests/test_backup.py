import sqlite3
from datetime import datetime, timedelta

import pytest

from voice_memory import backup, database as db


def _memory(db_path, audio=None, title="t"):
    return db.save_memory(title=title, transcript="hello world", audio_path=audio, db_path=db_path)


@pytest.fixture
def env(tmp_path, db_path):
    rec = tmp_path / "rec"
    (rec / "2026").mkdir(parents=True)
    (rec / "2026" / "a.ogg").write_bytes(b"audio-a")
    _memory(db_path, audio="2026/a.ogg")
    return db_path, rec, tmp_path / "bk"


def test_backup_then_verify_roundtrip(env):
    db_path, rec, dest = env
    r = backup.run_backup(db_path, rec, None, dest)
    assert r["recordings_copied"] == 1 and (dest / "recordings/2026/a.ogg").read_bytes() == b"audio-a"
    v = backup.verify(dest)
    assert v["memories"] == 1 and v["audio_files_checked"] == 1 and v["recent_ok"]


def test_snapshot_includes_uncheckpointed_wal_data(env):
    db_path, rec, dest = env
    holder = db.connect(db_path)  # keep a connection open so the WAL is not checkpointed away
    _memory(db_path, title="late")
    snap = backup.snapshot_db(db_path, dest)
    holder.close()
    assert backup._check(snap) == 2
    assert sorted(x.name for x in dest.iterdir()) == [snap.name]  # no -wal/-shm/.tmp leftovers


def test_recordings_are_incremental_and_never_deleted_from_backup(env):
    db_path, rec, dest = env
    backup.run_backup(db_path, rec, None, dest)
    (rec / "2026" / "b.ogg").write_bytes(b"b")
    (rec / "2026" / "a.ogg").unlink()  # source loses a file: backup keeps it
    r = backup.run_backup(db_path, rec, None, dest)
    assert r["recordings_copied"] == 1
    assert (dest / "recordings/2026/a.ogg").exists() and (dest / "recordings/2026/b.ogg").exists()


def test_truncated_copy_is_replaced(env):
    db_path, rec, dest = env
    backup.run_backup(db_path, rec, None, dest)
    (dest / "recordings/2026/a.ogg").write_bytes(b"x")  # size differs
    backup.run_backup(db_path, rec, None, dest)
    assert (dest / "recordings/2026/a.ogg").read_bytes() == b"audio-a"


def test_prune_keeps_newest(env):
    db_path, _, dest = env
    t = datetime(2026, 10, 1, 3, 0, 0)
    for i in range(5):
        backup.snapshot_db(db_path, dest, now=t + timedelta(days=i))
    assert len(backup.prune(dest, keep=2)) == 3
    assert [p.name for p in backup._snapshots(dest)][-1].startswith("voice_memory-20261005")
    assert len(backup._snapshots(dest)) == 2


def test_verify_detects_missing_audio_and_corruption(env):
    db_path, rec, dest = env
    backup.run_backup(db_path, rec, None, dest)
    (dest / "recordings/2026/a.ogg").unlink()
    with pytest.raises(backup.BackupError, match="missing"):
        backup.verify(dest)
    snap = backup._snapshots(dest)[-1]
    snap.write_bytes(snap.read_bytes()[:-4096] + b"\0" * 4096)
    with pytest.raises((backup.BackupError, sqlite3.DatabaseError)):
        backup.verify(dest)


def test_verify_without_snapshots_fails(tmp_path):
    with pytest.raises(backup.BackupError):
        backup.verify(tmp_path)


def test_main_exit_codes(env, monkeypatch, capsys):
    db_path, rec, dest = env
    monkeypatch.setenv("VOICE_MEMORY_DB", str(db_path))
    monkeypatch.setenv("VOICE_MEMORY_RECORDINGS", str(rec))
    monkeypatch.setenv("VOICE_MEMORY_CLIP_STATE", str(rec / "none.json"))
    assert backup.main(["--dest", str(dest)]) == 0
    assert backup.main(["--dest", str(dest), "--verify"]) == 0
    assert backup.main(["--dest", str(dest / "nope"), "--verify"]) == 1


def _fake_rclone(fail_on=None):
    import subprocess
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1 if cmd[1] == fail_on else 0, "", "boom")
    return runner, calls


def test_upload_never_syncs_recordings_with_delete(env):
    db_path, rec, dest = env
    backup.run_backup(db_path, rec, None, dest)
    runner, calls = _fake_rclone()
    backup.upload(dest, "gdrive:voice-memory", runner)
    assert [c[1] for c in calls] == ["sync", "copy", "check"]
    assert "--include" in calls[0] and calls[0][3] == "gdrive:voice-memory"  # sync is limited to snapshots/state
    assert calls[1][1] == "copy" and calls[1][3].endswith("/recordings")


@pytest.mark.parametrize("step", ["sync", "copy", "check"])
def test_upload_failure_is_reported(env, step):
    db_path, rec, dest = env
    backup.run_backup(db_path, rec, None, dest)
    runner, _ = _fake_rclone(fail_on=step)
    with pytest.raises(backup.BackupError, match=step):
        backup.upload(dest, "gdrive:vm", runner)


@pytest.mark.parametrize("bad", ["/tmp/x", "nocolon", ":path", "a/b:c"])
def test_upload_rejects_non_remote_targets(env, bad):
    _, _, dest = env
    with pytest.raises(backup.BackupError):
        backup.upload(dest, bad, _fake_rclone()[0])
