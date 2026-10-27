"""Re-transcribe past audio memories with the current Whisper default (non-destructive).

For each audio-ingest memory created before --before: old transcript/title/summary go to
transcript_history, then transcript, title, summary and the FTS row are replaced. Tags, ids,
registrations and suggestions are left alone. Re-runnable: memories already in history are skipped.

    python scripts/retranscribe.py --before 2026-10-25T09:40:00+00:00 [--limit N] [--no-summary]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from contextlib import closing

from voice_memory import database as db
from voice_memory.ingest import config
from voice_memory.ingest.cli import _setup_logging
from voice_memory.ingest.summarize import ClaudeSummarizer
from voice_memory.ingest.transcribe import WhisperTranscriber

log = logging.getLogger("voice_memory.ingest")

HISTORY = """
CREATE TABLE IF NOT EXISTS transcript_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id     TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    transcript    TEXT NOT NULL,
    title         TEXT NOT NULL,
    summary       TEXT NOT NULL,
    note          TEXT NOT NULL,
    replaced_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transcript_history_memory ON transcript_history(memory_id);
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", required=True, help="only memories created_at < this (ISO, UTC)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-summary", action="store_true")
    args = ap.parse_args()
    _setup_logging()
    config.load_dotenv()
    model = os.environ.get("VOICE_MEMORY_WHISPER_MODEL", config.DEFAULT_WHISPER_MODEL)
    transcriber = WhisperTranscriber(model)
    summarizer = None if args.no_summary else ClaudeSummarizer(
        os.environ.get("VOICE_MEMORY_CLAUDE_MODEL", config.DEFAULT_CLAUDE_MODEL))

    with closing(db.connect()) as conn:
        conn.executescript(HISTORY)
        rows = conn.execute(
            "SELECT id, audio_path, title FROM memories WHERE source='audio-ingest' AND audio_path IS NOT NULL "
            "AND created_at < ? AND id NOT IN (SELECT memory_id FROM transcript_history) ORDER BY recorded_at",
            (args.before,)).fetchall()
        if args.limit:
            rows = rows[: args.limit]
        log.info("re-transcribing %d memories with %s", len(rows), model)
        done = 0
        for r in rows:
            path = config.recordings_dir() / r["audio_path"]
            if not path.exists():
                log.warning("skip %s: audio missing (%s)", r["id"], path)
                continue
            try:
                new = transcriber.transcribe(path).strip()
                if not new or len(new) > db.MAX_TRANSCRIPT:
                    raise ValueError(f"unusable transcript ({len(new)} chars)")
                title, summary = r["title"], None
                if summarizer is not None:
                    tags = [t["name"] for t in db.list_tags()]
                    s = summarizer.summarize(new, tags)
                    title, summary = s.title or title, s.summary
            except Exception as e:  # keep going; the old text stays untouched
                log.error("skip %s: %s", r["id"], e)
                continue
            with conn:
                old = conn.execute("SELECT transcript, title, summary FROM memories WHERE id=?", (r["id"],)).fetchone()
                conn.execute(
                    "INSERT INTO transcript_history (memory_id, transcript, title, summary, note, replaced_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (r["id"], old["transcript"], old["title"], old["summary"],
                     f"before retranscribe with {model}", db._now()))
                summary = old["summary"] if summary is None else summary
                conn.execute("UPDATE memories SET transcript=?, title=?, summary=? WHERE id=?",
                             (new, title, summary, r["id"]))
                tags = " ".join(db._tags_for(conn, [r["id"]]).get(r["id"], []))
                conn.execute("DELETE FROM memories_fts WHERE memory_id=?", (r["id"],))
                conn.execute(
                    "INSERT INTO memories_fts (memory_id, title, summary, transcript, tags) VALUES (?,?,?,?,?)",
                    (r["id"], title, summary, new, tags))
                conn.execute("UPDATE ingested_files SET transcript=?, updated_at=? WHERE memory_id=?",
                             (new, db._now(), r["id"]))
            done += 1
            log.info("replaced %s (%d/%d): %s", r["id"], done, len(rows), title)
    return 0


if __name__ == "__main__":
    sys.exit(main())
