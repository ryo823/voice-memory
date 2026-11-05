"""Dry run: how would past audio memories split into conversation chunks? (Phase 2: never writes the DB.)

For each eligible memory (audio-ingest, not archived, not already split, audio file present, >= min duration)
the audio is re-transcribed with the production Whisper settings and cut with ingest/split.py. The timed
transcript is cached under --cache-dir (a JSON file per audio sha) so the real run can reuse it.
The DB is opened read-only. Memories whose audio file is missing are skipped and listed.

    resplit.py --dry-run                 all eligible memories, sparse recordings first
    resplit.py --dry-run --memory ID     one memory (repeatable)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

from voice_memory import database as db
from voice_memory.ingest import config
from voice_memory.ingest.split import SplitConfig, split_transcript
from voice_memory.ingest.transcribe import TimedTranscript, WhisperTranscriber

log = logging.getLogger("resplit")
DIFF_FLAG = 0.20  # flag when the new transcript length differs from the stored one by more than this


def eligible(conn: sqlite3.Connection, cfg: SplitConfig, only: list[str] | None):
    rows = conn.execute(
        "SELECT m.id, m.title, m.audio_path, length(m.transcript) AS chars, i.sha256, i.duration_sec "
        "FROM memories m JOIN ingested_files i ON i.stored_path = m.audio_path "
        "WHERE m.source = 'audio-ingest' AND m.archived = 0 AND m.split_method IS NULL "
        "AND m.audio_path IS NOT NULL AND m.parent_memory_id IS NULL ORDER BY i.duration_sec").fetchall()
    out, missing, short = [], [], 0
    for r in rows:
        if only and r["id"] not in only:
            continue
        if r["duration_sec"] < cfg.min_duration:
            short += 1
        elif not (config.recordings_dir() / r["audio_path"]).is_file():
            missing.append(r)
        else:
            out.append(r)
    return out, missing, short


def timed_for(row, cache_dir: Path, transcriber: WhisperTranscriber) -> tuple[TimedTranscript, bool]:
    cache = cache_dir / f"{row['sha256']}.json"
    if cache.is_file():
        return TimedTranscript.from_json(cache.read_text(encoding="utf-8")), True
    timed = transcriber.transcribe_timed(config.recordings_dir() / row["audio_path"])
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".part")
    tmp.write_text(timed.to_json(), encoding="utf-8")
    tmp.replace(cache)
    return timed, False


def hms(x: float) -> str:
    return f"{int(x) // 3600}:{int(x) % 3600 // 60:02d}:{int(x) % 60:02d}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", required=True, help="required; this script cannot write the DB yet")
    ap.add_argument("--memory", action="append", help="memory id (repeatable); default: all eligible")
    ap.add_argument("--cache-dir", type=Path, default=db.PROJECT_ROOT / "data" / "resplit-cache")
    ap.add_argument("--threads", type=int, default=3)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    config.load_dotenv()
    cfg = SplitConfig.from_env()
    model = os.environ.get("VOICE_MEMORY_WHISPER_MODEL", config.DEFAULT_WHISPER_MODEL)
    transcriber = WhisperTranscriber(model, threads=args.threads)

    uri = f"file:{db.default_db_path()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        rows, missing, short = eligible(conn, cfg, args.memory)
    log.info("eligible %d, audio missing %d, shorter than %.0fs %d (whisper %s)", len(rows), len(missing),
             cfg.min_duration, short, model)

    report = {"missing": [dict(r) for r in missing], "items": []}
    for r in rows:
        t0 = time.monotonic()
        timed, cached = timed_for(r, args.cache_dir, transcriber)
        chunks = split_transcript(timed, cfg)
        new_chars = len(timed.text)
        diff = (new_chars - r["chars"]) / r["chars"] if r["chars"] else None
        item = {"id": r["id"], "title": r["title"], "duration_sec": r["duration_sec"], "old_chars": r["chars"],
                "new_chars": new_chars, "diff": diff, "flag": diff is None or abs(diff) > DIFF_FLAG,
                "cached": cached, "elapsed_sec": round(time.monotonic() - t0),
                "chunks": [{"start": c.start, "end": c.end, "chars": len(c.text), "head": c.text[:40]} for c in chunks]}
        report["items"].append(item)
        log.info("%s %.0fmin: %d chars -> %d chunks (%s)", r["id"][:8], r["duration_sec"] / 60, r["chars"],
                 len(chunks), "cache" if cached else f"{item['elapsed_sec']}s")
        (args.cache_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
