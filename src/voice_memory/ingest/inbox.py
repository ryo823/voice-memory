"""voice-memory-inbox: ingest every audio file dropped into data/inbox/ (e.g. Voice Memos rsynced from a Mac).

Files are never deleted or moved. Duplicates are skipped by audio SHA-256 (process_file), so re-sending the
same recording is harmless. A file that fails is tried at most MAX_ATTEMPTS times (until its size changes).
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from .. import notify
from ..database import PROJECT_ROOT
from . import config
from .audio import InvalidAudioError
from .cli import _setup_logging
from .clip_sync import load_state, save_state
from .pipeline import IngestError, process_file
from .summarize import ClaudeSummarizer
from .transcribe import WhisperTranscriber

log = logging.getLogger("voice_memory.ingest")

MAX_ATTEMPTS = 3
SETTLE_SEC = 60  # ignore files modified more recently than this (still being copied)
EXTS = {".m4a", ".mp3", ".wav", ".ogg", ".opus", ".aac", ".flac", ".caf"}


def inbox_dir() -> Path:
    env = os.environ.get("VOICE_MEMORY_INBOX")
    return Path(env).expanduser() if env else PROJECT_ROOT / "data" / "inbox"


def state_path() -> Path:
    env = os.environ.get("VOICE_MEMORY_INBOX_STATE")
    return Path(env).expanduser() if env else PROJECT_ROOT / "data" / "inbox_state.json"


def candidates(inbox: Path, now: float) -> list[Path]:
    out = []
    for p in sorted(inbox.rglob("*")):
        if p.name.startswith(".") or p.suffix.lower() not in EXTS or not p.is_file():
            continue
        if now - p.stat().st_mtime >= SETTLE_SEC:
            out.append(p)
    return out


def run(inbox: Path, state: dict, ingest: Callable[[Path], dict], now: float | None = None) -> dict:
    """Returns {"done": [...], "skipped": n, "failed": [...]}. Mutates `state` (key: relative path)."""
    now = time.time() if now is None else now
    out: dict = {"done": [], "skipped": 0, "failed": []}
    for p in candidates(inbox, now):
        key, size = str(p.relative_to(inbox)), p.stat().st_size
        entry = state.get(key) or {}
        if entry.get("size") != size:
            entry = {"size": size, "attempts": 0}
        if entry.get("status") in ("done", "skipped") or entry["attempts"] >= MAX_ATTEMPTS:
            state[key] = entry
            continue
        entry["attempts"] += 1
        state[key] = entry
        try:
            result = ingest(p)
            entry.update(status=result["status"], memory_id=result.get("memory_id"), error=None)
            if result["status"] == "done":
                out["done"].append(key)
            else:
                out["skipped"] += 1
        except (IngestError, InvalidAudioError, subprocess.TimeoutExpired, OSError) as e:
            entry.update(status="failed", error=str(e)[:300])
            out["failed"].append(key)
            log.error("%s failed (attempt %d/%d): %s", key, entry["attempts"], MAX_ATTEMPTS, e)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="voice-memory-inbox", description=__doc__)
    ap.add_argument("--no-summary", action="store_true")
    args = ap.parse_args(argv)

    _setup_logging()
    config.load_dotenv()
    inbox = inbox_dir()
    inbox.mkdir(parents=True, exist_ok=True)
    path = state_path()
    with open(path.with_suffix(".lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("another voice-memory-inbox is running; exiting")
            return 0
        state = load_state(path)
        if not candidates(inbox, time.time()):
            return 0  # don't load Whisper for an empty inbox
        whisper = os.environ.get("VOICE_MEMORY_WHISPER_MODEL", config.DEFAULT_WHISPER_MODEL)
        claude = os.environ.get("VOICE_MEMORY_CLAUDE_MODEL", config.DEFAULT_CLAUDE_MODEL)
        transcriber = WhisperTranscriber(whisper)
        summarizer = None if args.no_summary else ClaudeSummarizer(claude)
        try:
            result = run(inbox, state, lambda p: process_file(p, transcriber, summarizer))
        finally:
            save_state(path, state)
    if result["failed"]:
        notify.notify("inbox", f"{len(result['failed'])} file(s) failed to ingest: "
                      f"{', '.join(result['failed'][:5])}; see logs/ingest.log")
    else:
        notify.resolve("inbox")
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
