"""Ingest one local audio file.

States in ingested_files (keyed by SHA-256 of the audio bytes):
  processing -> transcribed -> done      (failure at any step -> failed)
`done` files are skipped (no double registration). `failed`/`transcribed`/stale `processing`
rows are resumed; the cached transcript avoids re-running speech recognition.
The memory row(s) and the `done` mark are written in ONE transaction, so a crash can never leave
a memory without its ingest record (or the reverse). Source audio is never deleted.
A long file (>= split.min_duration) is split at pauses in speech into several memories, all written in that
same transaction: either every chunk exists and the file is `done`, or none does and the run resumes from the
cached timed transcript (ingested_files.segments_json), so no chunk can be lost behind the SHA-256 skip.
"""
from __future__ import annotations

import logging
import re
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import database as db
from . import audio, split
from .summarize import Summarizer, SummarizeError
from .transcribe import TimedTranscript, Transcriber, TranscribeError

log = logging.getLogger("voice_memory.ingest")

SOURCE = "audio-ingest"
# Voice Memos are named "YYYYMMDD HHMMSS-<id>.m4a" in the device's local time; mtime is unreliable for them
# (an iCloud bulk download stamps every old memo with the download time).
VOICEMEMO_NAME = re.compile(r"^(\d{8})[ _](\d{6})(?!\d)")
VOICEMEMO_TZ = timezone(timedelta(hours=9), "Asia/Tokyo")
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
    updated_at    TEXT NOT NULL,
    segments_json TEXT
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first release to an existing ingested_files table."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(ingested_files)")}
    if "segments_json" not in have:
        try:
            conn.execute("ALTER TABLE ingested_files ADD COLUMN segments_json TEXT")
            conn.commit()
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):
                raise


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
        if row["status"] in ("done", "deleted"):  # deleted = removed from the viewer; do not resurrect it
            return None
        if row["status"] == "processing":
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["updated_at"])).total_seconds()
            if age < STALE_PROCESSING_SEC:
                return None  # another run is working on it
        conn.execute("UPDATE ingested_files SET status = ?, error = NULL, updated_at = ? WHERE sha256 = ?",
                     ("transcribed" if row["transcript"] else "processing", now, sha))
        return row


def recorded_at_for(src: Path) -> str:
    """Recording time: the Voice Memos file-name timestamp (as JST) if it parses, else the file's mtime (UTC)."""
    m = VOICEMEMO_NAME.match(src.name)
    if m:
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(
                tzinfo=VOICEMEMO_TZ).astimezone(timezone.utc).isoformat(timespec="seconds")
        except ValueError:
            pass
    return datetime.fromtimestamp(src.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")


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


def chunk_recorded_at(src: Path, base: str, start_sec: float) -> str:
    """Recording time of a chunk: Voice Memos name stamps are the recording start, so add the offset into the file.
    Other sources (file mtime) keep the file's time for every chunk."""
    if not VOICEMEMO_NAME.match(src.name):
        return base
    return (datetime.fromisoformat(base) + timedelta(seconds=start_sec)).isoformat(timespec="seconds")


def process_file(
    path: Path | str,
    transcriber: Transcriber,
    summarizer: Summarizer | None,
    *,
    db_path: Path | str | None = None,
    recordings: Path | None = None,
    split_config: split.SplitConfig | None = None,
) -> dict:
    """Returns {"status": "done"|"skipped", ...}. Raises audio.InvalidAudioError or IngestError."""
    from .config import recordings_dir

    src = Path(path).expanduser().resolve()
    recordings = recordings or recordings_dir()
    info = audio.probe(src)  # raises InvalidAudioError before anything is stored
    sha = audio.sha256_file(src)

    with closing(db.connect(db_path)) as conn:
        db.ensure_schema(conn)
        db.ensure_fts(conn)  # ingest bypasses init_db; index an old DB before save_memory writes to FTS
        conn.executescript(SCHEMA)
        _migrate(conn)
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
            cfg = split_config or split.SplitConfig.from_env()
            transcript = row.get("transcript")
            timed = TimedTranscript.from_json(row["segments_json"]) if row.get("segments_json") else None
            if transcript is None:
                log.info("transcribing %s (%.0fs audio)", src.name, info.duration_sec)
                timed_fn = getattr(transcriber, "transcribe_timed", None)
                if timed_fn is not None:
                    timed = timed_fn(src)
                    transcript = timed.text.strip()
                else:
                    transcript = transcriber.transcribe(src).strip()
                if not transcript:
                    raise IngestError("transcription was empty (silence or unintelligible audio)")
                cols = dict(transcript=transcript, status="transcribed")
                if timed is not None:
                    cols["segments_json"] = timed.to_json()
                _set(conn, sha, **cols)

            chunks: list[split.Chunk] = []
            if timed is not None and cfg.enabled and info.duration_sec >= cfg.min_duration:
                chunks = split.split_transcript(timed, cfg)
            if len(chunks) <= 1:
                chunks = []
                if len(transcript) > db.MAX_TRANSCRIPT:
                    raise IngestError(f"transcript is {len(transcript)} chars, over the {db.MAX_TRANSCRIPT} limit")

            existing = [t["name"] for t in db.list_tags(db_path)] if summarizer is not None else []
            recorded_at = recorded_at_for(src)
            parts = chunks or [None]
            prepared = []
            for i, ch in enumerate(parts):
                text = ch.text if ch else transcript
                title, summary, tags = "", "", []
                if summarizer is not None:
                    s_ = summarizer.summarize(text, existing)
                    title, summary, tags = s_.title, s_.summary, s_.tags
                if not title:
                    label = f"{src.stem} ({datetime.now().strftime('%Y-%m-%d %H:%M')})"
                    title = (f"{label} [{i + 1}/{len(parts)}]" if ch else label)[: db.MAX_TITLE]
                prepared.append((ch, text, title, summary, tags))

            saved_all = []
            with conn:  # every memory + the done mark: one transaction
                for i, (ch, text, title, summary, tags) in enumerate(prepared):
                    extra = {}
                    if ch is not None:
                        extra = dict(audio_start_sec=round(ch.start, 1), audio_end_sec=round(ch.end, 1),
                                     split_method=split.METHOD, file_sha256=sha, segment_index=i)
                    saved_all.append(db.save_memory(
                        title, text, summary, tags, source=SOURCE,
                        recorded_at=chunk_recorded_at(src, recorded_at, ch.start) if ch else recorded_at,
                        audio_path=stored, conn=conn, **extra,
                    ))
                conn.execute(
                    "UPDATE ingested_files SET status='done', memory_id=?, error=NULL, updated_at=? WHERE sha256=?",
                    (saved_all[0]["id"], _now(), sha),
                )
            log.info("saved %d memor%s from %s", len(saved_all), "y" if len(saved_all) == 1 else "ies", src.name)
            first = saved_all[0]
            return {"status": "done", "sha256": sha, "memory_id": first["id"],
                    "memory_ids": [m["id"] for m in saved_all], "chunks": len(saved_all),
                    "title": first["title"], "tags": first["tags"], "audio_path": stored}
        except (TranscribeError, SummarizeError, IngestError, db.ValidationError, sqlite3.Error) as e:
            _set(conn, sha, error=f"{type(e).__name__}: {e}"[:1000],
                 status="transcribed" if _has_transcript(conn, sha) else "failed")
            log.error("failed %s: %s (audio kept at %s; rerun to resume)", src.name, e, recordings / stored)
            raise IngestError(str(e)) from e


def _has_transcript(conn: sqlite3.Connection, sha: str) -> bool:
    r = conn.execute("SELECT transcript FROM ingested_files WHERE sha256 = ?", (sha,)).fetchone()
    return bool(r and r["transcript"])
