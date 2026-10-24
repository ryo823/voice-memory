"""voice-memory-clip-sync: fetch new reSpeaker Clip sessions and ingest them.

Meant to run from a timer. If the Clip is out of BLE range it exits quietly; Wi-Fi (ClipAP) is only
touched when there is a new session to fetch. Nothing is ever deleted from the Clip.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..database import PROJECT_ROOT
from . import config
from .audio import InvalidAudioError
from .cli import _setup_logging
from .pipeline import IngestError, process_file
from .summarize import ClaudeSummarizer
from .transcribe import WhisperTranscriber

log = logging.getLogger("voice_memory.ingest")

CLIP_MAC = "C3:31:EB:7C:F4:8D"
SDK = Path.home() / "clip-sdk"
MAX_ATTEMPTS = 3
SESSION_RE = re.compile(r"^\s+(\d{14}):")

Runner = Callable[[list[str], int], subprocess.CompletedProcess]


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def state_path() -> Path:
    env = os.environ.get("VOICE_MEMORY_CLIP_STATE")
    return Path(env).expanduser() if env else PROJECT_ROOT / "data" / "clip_sync_state.json"


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def parse_sessions(output: str) -> list[str]:
    return [m.group(1) for line in output.splitlines() if (m := SESSION_RE.match(line))]


def parse_clip_state(output: str) -> str | None:
    m = re.search(r"^State:\s*(\w+)", output, re.M)
    return m.group(1) if m else None


@dataclass
class Clip:
    runner: Runner = _run
    sdk: Path = SDK
    mac: str = CLIP_MAC

    def _cli(self, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
        cmd = [str(self.sdk / "venv/bin/python"), str(self.sdk / "src/tools/clip-cli.py"),
               "--device", self.mac, "--timeout", "20", *args]
        return self.runner(cmd, timeout)

    def status(self) -> str | None:
        r = self._cli("status")
        return parse_clip_state(r.stdout) if r.returncode == 0 else None

    def sessions(self) -> list[str] | None:
        r = self._cli("list")
        return parse_sessions(r.stdout) if r.returncode == 0 else None

    def fetch(self, session: str, script: Path) -> Path | None:
        self.runner([str(script), session], 400)
        ogg = self.sdk / "recordings" / "Clip" / session / f"{session}.ogg"
        return ogg if ogg.is_file() and ogg.stat().st_size > 0 else None


def sync(clip: Clip, state: dict, ingest: Callable[[Path], dict], script: Path, dry_run: bool = False) -> dict:
    """Returns {"clip": state|None, "new": [...], "done": [...], "failed": [...]}. Mutates `state`."""
    out: dict = {"clip": None, "new": [], "done": [], "failed": []}
    out["clip"] = clip.status()
    if out["clip"] is None:
        log.info("Clip not reachable over BLE; nothing to do")
        return out
    if out["clip"] != "IDLE":
        log.info("Clip is %s; not touching it", out["clip"])
        return out
    sessions = clip.sessions()
    if sessions is None:
        log.warning("could not list Clip sessions")
        return out
    for s in sessions:
        entry = state.get(s, {})
        if entry.get("status") in ("done", "ignored") or entry.get("attempts", 0) >= MAX_ATTEMPTS:
            continue
        out["new"].append(s)
    if dry_run:
        return out
    for s in out["new"]:
        entry = state.setdefault(s, {})
        entry["attempts"] = entry.get("attempts", 0) + 1
        try:
            ogg = clip.fetch(s, script)
            if ogg is None:
                raise IngestError("fetch produced no .ogg")
            result = ingest(ogg)
            entry.update(status="done", memory_id=result.get("memory_id"), error=None)
            out["done"].append(s)
        except (IngestError, InvalidAudioError, subprocess.TimeoutExpired, OSError) as e:
            entry.update(status="failed", error=str(e)[:300])
            out["failed"].append(s)
            log.error("session %s failed (attempt %d/%d): %s", s, entry["attempts"], MAX_ATTEMPTS, e)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="voice-memory-clip-sync", description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="only report new sessions")
    ap.add_argument("--ignore", nargs="+", metavar="SESSION", help="mark sessions as never to ingest, then exit")
    ap.add_argument("--retry", nargs="+", metavar="SESSION", help="reset failed sessions so they are tried again")
    ap.add_argument("--no-summary", action="store_true")
    args = ap.parse_args(argv)

    _setup_logging()
    config.load_dotenv()
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("another clip-sync is running; exiting")
            return 0
        state = load_state(path)
        for s in args.ignore or []:
            state[s] = {"status": "ignored"}
        for s in args.retry or []:
            state.pop(s, None)
        if args.ignore or args.retry:
            save_state(path, state)
            return 0

        whisper = os.environ.get("VOICE_MEMORY_WHISPER_MODEL", config.DEFAULT_WHISPER_MODEL)
        claude = os.environ.get("VOICE_MEMORY_CLAUDE_MODEL", config.DEFAULT_CLAUDE_MODEL)
        transcriber = WhisperTranscriber(whisper)
        summarizer = None if args.no_summary else ClaudeSummarizer(claude)
        script = PROJECT_ROOT / "scripts" / "clip_wifi_sync.sh"
        try:
            result = sync(Clip(), state, lambda p: process_file(p, transcriber, summarizer), script, args.dry_run)
        finally:
            if not args.dry_run:
                save_state(path, state)
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
