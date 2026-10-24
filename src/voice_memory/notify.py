"""Failure notifications to a Discord webhook (VOICE_MEMORY_DISCORD_WEBHOOK, normally in .env).

Messages are short status lines only: never transcripts, summaries or file contents, and the webhook URL
is never logged. A failing `key` notifies once, repeats at most every `cooldown` seconds, and a recovery
message is sent when `resolve(key)` is called. Sending never raises: a broken notifier must not break a backup.

    voice-memory-notify --test             send a test message
    voice-memory-notify --unit NAME        used by systemd OnFailure= (voice-memory-notify@.service)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable

from . import database as db
from .ingest import config

log = logging.getLogger("voice_memory.notify")
ENV = "VOICE_MEMORY_DISCORD_WEBHOOK"
COOLDOWN = 6 * 3600


def state_path() -> Path:
    env = os.environ.get("VOICE_MEMORY_NOTIFY_STATE")
    return Path(env).expanduser() if env else db.PROJECT_ROOT / "data" / "notify_state.json"


def _post(url: str, text: str) -> bool:
    body = json.dumps({"content": text[:1800]}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": "voice-memory-notify/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except Exception as e:  # noqa: BLE001 - never raise; str(e) of URLError/HTTPError does not contain the URL path
        log.warning("notification failed: %s", type(e).__name__)
        return False


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        log.warning("could not save notify state: %s", e)


def _send(text: str, post: Callable[[str, str], bool]) -> bool:
    url = os.environ.get(ENV)
    if not url:
        log.info("%s not set; notification skipped: %s", ENV, text)
        return False
    return (post or _post)(url, f"[voice-memory@{socket.gethostname()}] {text}")


def notify(key: str, message: str, *, cooldown: int = COOLDOWN, now: float | None = None,
           path: Path | None = None, post: Callable[[str, str], bool] | None = None) -> bool:
    """Report that `key` is failing. Returns True if a message was sent."""
    now = time.time() if now is None else now
    path = path or state_path()
    state = _load(path)
    entry = state.get(key)
    if entry and now - entry.get("last_sent", 0) < cooldown:
        return False
    text = message if not entry else f"{message} (still failing since {time.strftime('%m/%d %H:%M', time.localtime(entry['since']))})"
    if not _send(f"FAILED {key}: {text}", post):
        return False  # not recorded, so the next failure retries the notification
    state[key] = {"since": entry["since"] if entry else now, "last_sent": now}
    _save(path, state)
    return True


def resolve(key: str, *, path: Path | None = None, post: Callable[[str, str], bool] | None = None) -> bool:
    """Report recovery if `key` was failing. Returns True if a message was sent."""
    path = path or state_path()
    state = _load(path)
    if key not in state:
        return False
    _send(f"RECOVERED {key}", post)
    state.pop(key)
    _save(path, state)
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="voice-memory-notify", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--test", action="store_true", help="send a test message")
    g.add_argument("--unit", metavar="NAME", help="report that this systemd unit failed")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config.load_dotenv()
    if args.test:
        ok = _send("test notification", _post)
        print("sent" if ok else f"not sent (is {ENV} set in .env?)")
        return 0 if ok else 1
    notify(f"unit:{args.unit}", f"systemd unit {args.unit} failed; see: journalctl --user -u {args.unit}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
