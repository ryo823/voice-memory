"""voice-memory-backup: consistent DB snapshot + incremental copy of the recordings, and a restore check.

    voice-memory-backup              snapshot the DB, copy new recordings, prune old snapshots
    voice-memory-backup --verify     restore the newest snapshot into a temp dir and check it

Snapshots use SQLite's online backup API (safe while the WAL DB is in use; no sqlite3 CLI needed).
Recordings are only ever added to the destination, never deleted from it or from the source.
The destination should be on a different disk/host than data/ to survive a disk failure.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from . import database as db
from .ingest.config import recordings_dir

log = logging.getLogger("voice_memory.backup")
KEEP_DEFAULT = 14
PREFIX = "voice_memory-"


class BackupError(RuntimeError):
    pass


def upload(dest: Path, remote: str, runner=subprocess.run) -> None:
    """Mirror the backup dir to an rclone remote ("name:path"). Recordings are copied, never deleted remotely;
    DB snapshots (and the sync state) are synced so remote pruning follows the local one."""
    name, sep, _ = remote.partition(":")
    if not sep or not name or "/" in name:
        raise BackupError(f"rclone remote must look like 'name:path', got {remote!r}")
    base = remote.rstrip("/")
    steps = [
        ["rclone", "sync", str(dest), base, "--include", f"{PREFIX}*.db", "--include", "clip_sync_state.json"],
        ["rclone", "copy", str(dest / "recordings"), f"{base}/recordings"],
        ["rclone", "check", "--one-way", str(dest / "recordings"), f"{base}/recordings"],
    ]
    for cmd in steps:
        if cmd[1] != "sync" and not (dest / "recordings").is_dir():
            continue
        try:
            r = runner(cmd, capture_output=True, text=True, timeout=3600)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise BackupError(f"rclone {cmd[1]} failed: {e}") from e
        if r.returncode != 0:
            raise BackupError(f"rclone {cmd[1]} exited {r.returncode}: {(r.stderr or r.stdout)[-300:]}")


def backup_dir() -> Path:
    env = os.environ.get("VOICE_MEMORY_BACKUP_DIR")
    return Path(env).expanduser() if env else Path.home() / "backups" / "voice-memory"


def _snapshots(dest: Path) -> list[Path]:
    return sorted(dest.glob(f"{PREFIX}*.db"))


def _check(path: Path) -> int:
    """integrity_check a DB file read-only; return the memory count."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        res = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if res != "ok":
            raise BackupError(f"integrity_check failed for {path}: {res}")
        return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    finally:
        conn.close()


def snapshot_db(src: Path, dest: Path, now: datetime | None = None) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / f"{PREFIX}{(now or datetime.now()):%Y%m%d-%H%M%S}.db"
    tmp = target.with_suffix(".db.tmp")
    tmp.unlink(missing_ok=True)
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    d = sqlite3.connect(tmp)
    try:
        s.backup(d)
        d.execute("PRAGMA journal_mode = DELETE")  # a self-contained single file, no -wal/-shm beside it
    finally:
        d.close()
        s.close()
    try:
        _check(tmp)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    tmp.chmod(0o600)
    tmp.replace(target)
    return target


def copy_new_files(src: Path, dest: Path) -> tuple[int, int]:
    """Copy files missing from dest or with a different size. Returns (copied, total)."""
    copied = total = 0
    for f in sorted(p for p in src.rglob("*") if p.is_file() and p.name != ".gitkeep"):
        total += 1
        t = dest / f.relative_to(src)
        if t.exists() and t.stat().st_size == f.stat().st_size:
            continue
        t.parent.mkdir(parents=True, exist_ok=True)
        part = t.with_name(t.name + ".part")
        shutil.copy2(f, part)
        part.chmod(0o600)
        part.replace(t)
        copied += 1
    return copied, total


def prune(dest: Path, keep: int) -> list[Path]:
    old = _snapshots(dest)[:-keep] if keep > 0 else []
    for p in old:
        p.unlink()
    return old


def run_backup(src_db: Path, rec_dir: Path, state: Path | None, dest: Path, keep: int = KEEP_DEFAULT) -> dict:
    if not src_db.is_file():
        raise BackupError(f"database not found: {src_db}")
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    snap = snapshot_db(src_db, dest)
    copied, total = copy_new_files(rec_dir, dest / "recordings") if rec_dir.is_dir() else (0, 0)
    if state and state.is_file():
        shutil.copy2(state, dest / state.name)
    pruned = prune(dest, keep)
    return {"snapshot": str(snap), "recordings_copied": copied, "recordings_total": total,
            "pruned": len(pruned)}


def verify(dest: Path) -> dict:
    """Restore the newest snapshot to a temp dir; check integrity, usability and that audio files exist."""
    snaps = _snapshots(dest)
    if not snaps:
        raise BackupError(f"no snapshots in {dest}")
    snap = snaps[-1]
    with tempfile.TemporaryDirectory() as td:
        restored = Path(td) / "restored.db"
        shutil.copy2(snap, restored)
        count = _check(restored)
        recent = db.recent_memories(limit=1, db_path=restored)  # the app's own code path can open it
        paths = [r[0] for r in sqlite3.connect(f"file:{restored}?mode=ro", uri=True)
                 .execute("SELECT audio_path FROM memories WHERE audio_path IS NOT NULL")]
    missing = [p for p in paths if not (dest / "recordings" / p).is_file()]
    if missing:
        raise BackupError(f"{len(missing)} recording(s) referenced by {snap.name} missing from backup: {missing[:3]}")
    return {"snapshot": snap.name, "memories": count, "recent_ok": bool(recent) or count == 0,
            "audio_files_checked": len(paths)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="voice-memory-backup", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", type=Path, default=None, help="default: $VOICE_MEMORY_BACKUP_DIR or ~/backups/voice-memory")
    ap.add_argument("--keep", type=int, default=KEEP_DEFAULT, help="DB snapshots to keep (default %(default)s)")
    ap.add_argument("--rclone-remote", default=os.environ.get("VOICE_MEMORY_RCLONE_REMOTE"), metavar="NAME:PATH",
                    help="also upload the backup to this rclone remote (default: $VOICE_MEMORY_RCLONE_REMOTE)")
    ap.add_argument("--verify", action="store_true", help="restore-check the newest snapshot instead of backing up")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    dest = (args.dest or backup_dir()).expanduser()
    try:
        if args.verify:
            result = verify(dest)
        else:
            state = os.environ.get("VOICE_MEMORY_CLIP_STATE")
            state_p = Path(state).expanduser() if state else db.PROJECT_ROOT / "data" / "clip_sync_state.json"
            result = run_backup(db.default_db_path(), recordings_dir(), state_p, dest, args.keep)
            if args.rclone_remote:
                upload(dest, args.rclone_remote)
                result["uploaded_to"] = args.rclone_remote
    except (BackupError, sqlite3.Error, OSError) as e:
        log.error("%s", e)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
