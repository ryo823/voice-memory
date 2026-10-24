"""Ingest one local audio file.

States in ingested_files (keyed by SHA-256 of the audio bytes):
  processing -> transcribed -> done      (failure at any step -> failed)
`done` files are skipped (no double registration). `failed`/`transcribed`/stale `processing`
rows are resumed; the cached transcript avoids re-running speech recognition.
The memory row and the `done` mark are written in ONE transaction, so a crash can never leave
a memory without its ingest record (or the reverse). Source audio is never deleted.
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .. import database as db
from . import audio
from .summarize import Summarizer, SummarizeError
from .transcribe import Transcriber, TranscribeError

log = logging.getLogger("voice_memory.ingest")

SOURCE = "audio-ingest"
STALE_PROCESSING_SEC = 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingested_files (
    sha256        TEXT PRIMARY KEY,
    original_name TEXT NOT NULL,
    stored_path   TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    duration_sec  REAL NOT NULL,
    status        TEXT NOT NULL,
    transcript    TEXT,
    error         TEXT,
    memory_id     TEXT REFERENCES memories(id) ON DELETE SET NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
"""


class IngestError(RuntimeError):
    """Processing failed; the source file and the stored copy are kept and the run can be retried."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_name(name: str) -> str:
    keep = "".join(c if (c.isalnum() or c in "._-") else "_" for c in name)
    return keep[-80:] or "audio"


def _set(conn: sqlite3.Connection, sha: str, **cols) -> None:
    cols["updated_at"] = _now()
    sets = ", ".join(f"{k} = ?" for k in cols)
    with conn:
        conn.execute(f"UPDATE ingested_files SET {sets} WHERE sha256 = ?", (*cols.values(), sha))


def _claim(conn: sqlite3.Connection, sha: str, name: str, stored: str, info: audio.AudioInfo) -> dict | None:
    """Return None if the file is already done / being processed; else the (cached) row."""
    now = _now()
    with conn:
        inserted = conn.execute(
            "INSERT OR IGNORE INTO ingested_files "
            "(sha256, original_name, stored_path, size_bytes, duration_sec, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,'processing',?,?)",
            (sha, name, stored, info.size_bytes, info.duration_sec, now, now),
        ).rowcount == 1
        row = dict(conn.execute("SELECT * FROM ingested_files WHERE sha256 = ?", (sha,)).fetchone())
        if inserted:
            return row
        if row["status"] == "done":
            return None
        if row["status"] == "processing":
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["updated_at"])).total_seconds()
            if age < STALE_PROCESSING_SEC:
                return None  # another run is working on it
        conn.execute("UPDATE ingested_files SET status = ?, error = NULL, updated_at = ? WHERE sha256 = ?",
                     ("transcribed" if row["transcript"] else "processing", now, sha))
        return row


def _store_copy(src: Path, sha: str, recordings: Path) -> str:
    """Copy into recordings/YYYY/<sha12>_<name>; returns the path relative to `recordings`."""
    year = datetime.fromtimestamp(src.stat().st_mtime, timezone.utc).strftime("%Y")
    rel = Path(year) / f"{sha[:12]}_{_safe_name(src.name)}"
    dest = recordings / rel
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        shutil.copy2(src, tmp)
        tmp.replace(dest)
    return str(rel)


def process_file(
    path: Path | str,
    transcriber: Transcriber,
    summarizer: Summarizer | None,
    *,
    db_path: Path | str | None = None,
    recordings: Path | None = None,
) -> dict:
    """Returns {"status": "done"|"skipped", ...}. Raises audio.InvalidAudioError or IngestError."""
    from .config import recordings_dir

    src = Path(path).expanduser().resolve()
    recordings = recordings or recordings_dir()
    info = audio.probe(src)  # raises InvalidAudioError before anything is stored
    sha = audio.sha256_file(src)

    with closing(db.connect(db_path)) as conn:
        conn.executescript(db.SCHEMA)
        conn.executescript(SCHEMA)
        try:
            stored = _store_copy(src, sha, recordings)
        except OSError as e:
            raise IngestError(f"could not copy audio into {recordings}: {e}") from e
        row = _claim(conn, sha, src.name, stored, info)
        if row is None:
            mid = conn.execute("SELECT memory_id, status FROM ingested_files WHERE sha256 = ?", (sha,)).fetchone()
            log.info("skip %s (sha256 %s…, status %s)", src.name, sha[:12], mid["status"])
            return {"status": "skipped", "sha256": sha, "memory_id": mid["memory_id"], "reason": mid["status"]}
        try:
            transcript = row.get("transcript")
            if transcript is None:
                log.info("transcribing %s (%.0fs audio)", src.name, info.duration_sec)
                transcript = transcriber.transcribe(src).strip()
                if not transcript:
                    raise IngestError("transcription was empty (silence or unintelligible audio)")
                if len(transcript) > db.MAX_TRANSCRIPT:
                    raise IngestError(f"transcript is {len(transcript)} chars, over the {db.MAX_TRANSCRIPT} limit")
                _set(conn, sha, transcript=transcript, status="transcribed")

            title, summary, tags = "", "", []
            if summarizer is not None:
                existing = [t["name"] for t in db.list_tags(db_path)]
                s = summarizer.summarize(transcript, existing)
                title, summary, tags = s.title, s.summary, s.tags
            if not title:
                title = f"{src.stem} ({datetime.now().strftime('%Y-%m-%d %H:%M')})"[: db.MAX_TITLE]

            recorded_at = datetime.fromtimestamp(src.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
            with conn:  # memory + done mark: one transaction
                saved = db.save_memory(
                    title, transcript, summary, tags, source=SOURCE,
                    recorded_at=recorded_at, audio_path=stored, conn=conn,
                )
                conn.execute(
                    "UPDATE ingested_files SET status='done', memory_id=?, error=NULL, updated_at=? WHERE sha256=?",
                    (saved["id"], _now(), sha),
                )
            log.info("saved memory %s (%d tags)", saved["id"], len(tags))
            return {"status": "done", "sha256": sha, "memory_id": saved["id"], "title": title,
                    "tags": saved["tags"], "audio_path": stored}
        except (TranscribeError, SummarizeError, IngestError, db.ValidationError, sqlite3.Error) as e:
            _set(conn, sha, error=f"{type(e).__name__}: {e}"[:1000],
                 status="transcribed" if _has_transcript(conn, sha) else "failed")
            log.error("failed %s: %s (audio kept at %s; rerun to resume)", src.name, e, recordings / stored)
            raise IngestError(str(e)) from e


def _has_transcript(conn: sqlite3.Connection, sha: str) -> bool:
    r = conn.execute("SELECT transcript FROM ingested_files WHERE sha256 = ?", (sha,)).fetchone()
    return bool(r and r["transcript"])
