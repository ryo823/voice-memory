"""voice-memory-clip-sync: fetch new reSpeaker Clip sessions and ingest them.

Meant to run from a timer. If the Clip is out of BLE range it exits quietly; Wi-Fi (ClipAP) is only
touched when there is a new session to fetch. Nothing is ever deleted from the Clip.

Recording follows the charger: a Clip that is recording while charging is stopped (so the session is
finalized and fetched); an idle Clip that is off the charger is set recording again after the fetch.
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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import notify
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
UNREACHABLE_AFTER = 12 * 3600  # notify when the Clip has not answered over BLE for this long
CONTROL = PROJECT_ROOT / "scripts" / "clip_control.py"
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


def parse_clip_info(output: str) -> dict | None:
    try:
        info = json.loads(output.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    return info if isinstance(info, dict) and "state" in info else None


@dataclass
class Clip:
    runner: Runner = _run
    sdk: Path = SDK
    mac: str = CLIP_MAC

    def _cli(self, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
        cmd = [str(self.sdk / "venv/bin/python"), str(self.sdk / "src/tools/clip-cli.py"),
               "--device", self.mac, "--timeout", "20", *args]
        return self.runner(cmd, timeout)

    def control(self, action: str) -> dict | None:
        """action: state | stop | start. Returns {"state", "charging", "battery"} after the action."""
        cmd = [str(self.sdk / "venv/bin/python"), str(CONTROL), self.mac, action]
        r = self.runner(cmd, 60)
        return parse_clip_info(r.stdout) if r.returncode == 0 else None

    def sessions(self) -> list[str] | None:
        r = self._cli("list")
        return parse_sessions(r.stdout) if r.returncode == 0 else None

    def fetch(self, session: str, script: Path) -> Path | None:
        ogg = self.sdk / "recordings" / "Clip" / session / f"{session}.ogg"
        ogg.unlink(missing_ok=True)  # never mistake a partial file from an earlier attempt for this fetch
        r = self.runner([str(script), session], 400)
        if r.returncode != 0:
            log.warning("fetch script for %s exited %d: %s", session, r.returncode, (r.stdout or "")[-300:])
            return None
        return ogg if ogg.is_file() and ogg.stat().st_size > 0 else None


def sync(clip: Clip, state: dict, ingest: Callable[[Path], dict], script: Path, dry_run: bool = False,
         follow_charger: bool = True) -> dict:
    """Returns {"clip": state|None, "new": [...], "done": [...], "failed": [...]}. Mutates `state`."""
    out: dict = {"clip": None, "new": [], "done": [], "failed": []}
    info = clip.control("state")
    out["clip"] = info["state"] if info else None
    if info is None:
        log.info("Clip not reachable over BLE; nothing to do")
        return out
    if info["state"] == "RECORDING" and info.get("charging") and follow_charger and not dry_run:
        log.info("Clip is recording on the charger; stopping the recording")
        info = clip.control("stop") or info
        out["clip"] = info["state"]
        out["stopped"] = info["state"] == "IDLE"
    if info["state"] != "IDLE":
        log.info("Clip is %s; not touching it", info["state"])
        return out
    resume = follow_charger and not dry_run and not info.get("charging")
    try:
        _fetch_new(clip, state, ingest, script, dry_run, out)
    finally:
        if resume:
            _resume_recording(clip, out)
    return out


def _resume_recording(clip: Clip, out: dict) -> None:
    # Right after a Wi-Fi fetch the Clip is briefly WIFI_SYNC and may refuse; the next run retries.
    info = clip.control("start")
    out["resumed"] = bool(info and info["state"] == "RECORDING")
    if not out["resumed"]:
        log.warning("could not restart recording (clip: %s)", info and info["state"])


def _fetch_new(clip: Clip, state: dict, ingest: Callable[[Path], dict], script: Path, dry_run: bool,
               out: dict) -> None:
    sessions = clip.sessions()
    if sessions is None:
        log.warning("could not list Clip sessions")
        return
    for s in sessions:
        entry = state.get(s, {})
        if entry.get("status") in ("done", "ignored") or entry.get("attempts", 0) >= MAX_ATTEMPTS:
            continue
        out["new"].append(s)
    if dry_run:
        return
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


def _report(result: dict, state: dict, now: float | None = None) -> None:
    """Failure notifications. Never includes transcripts or error details, only counts and session ids."""
    now = time.time() if now is None else now
    if result["failed"]:
        notify.notify("clip-sync", f"{len(result['failed'])} session(s) failed to ingest: "
                      f"{', '.join(result['failed'][:5])}; see logs/ingest.log")
    elif result["done"] or not result["new"]:
        notify.resolve("clip-sync")
    meta = state.setdefault("_meta", {})
    if result["clip"] is not None:
        meta["last_reachable"] = now
        notify.resolve("clip-unreachable")
    else:
        last = meta.setdefault("last_reachable", now)
        if now - last > UNREACHABLE_AFTER:
            notify.notify("clip-unreachable", f"Clip not reachable over BLE for {int((now - last) // 3600)}h")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="voice-memory-clip-sync", description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="only report new sessions")
    ap.add_argument("--ignore", nargs="+", metavar="SESSION", help="mark sessions as never to ingest, then exit")
    ap.add_argument("--retry", nargs="+", metavar="SESSION", help="reset failed sessions so they are tried again")
    ap.add_argument("--no-summary", action="store_true")
    ap.add_argument("--no-charger-control", action="store_true",
                    help="never stop/start recording based on the charger")
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
            result = sync(Clip(), state, lambda p: process_file(p, transcriber, summarizer), script, args.dry_run,
                          not args.no_charger_control)
            if not args.dry_run:
                _report(result, state)
        finally:
            if not args.dry_run:
                save_state(path, state)
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
