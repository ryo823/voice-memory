"""voice-memory-suggest: daily proposal-only run of the suggest-actions skill.

    voice-memory-suggest              mail candidates for memos not yet mailed, then mark them as mailed
    voice-memory-suggest --dry-run    print the mail body instead of sending; marks nothing

Claude only gets read tools plus Gmail send_message (a memo's text cannot make the run touch Calendar or the DB).
The "already mailed" marks are written here, not by Claude, and only for the memo ids that Claude's own
get_pending_action_memories call returned, after the mail was sent (or after it decided nothing was mailable).
A failed run therefore marks nothing and the same memos are retried the next day.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from . import database as db

log = logging.getLogger("voice_memory.suggest")

ALLOWED_TOOLS = ",".join([
    "mcp__voice-memory__get_pending_action_memories",
    "mcp__voice-memory__get_voice_memory",
    "mcp__claude_ai_Gmail__send_message",
])
PROMPT = (
    "suggest-actions スキルを提案専用モードで実行して。"
    "get_pending_action_memories は exclude_suggested=true, limit=50 で呼ぶこと。"
    "最後の行は、メールを送ったら「RESULT: sent」、除外の結果メールを送らなかったら「RESULT: none」とだけ書く。"
)
DRY_RUN_PROMPT = " メールは送らず、本文を標準出力に出すだけにして。"
TIMEOUT = 14 * 60


class SuggestError(RuntimeError):
    pass


def claude_bin() -> str:
    return os.environ.get("VOICE_MEMORY_CLAUDE_BIN") or str(Path.home() / ".local" / "bin" / "claude")


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def parse_run(stdout: str) -> dict:
    """Read `claude -p --output-format stream-json` output: which tools ran, what they returned, the final text."""
    names: dict[str, str] = {}
    out = {"pending_ids": [], "send_attempts": 0, "send_ok": 0, "result": None, "result_error": False}
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "result":
            out["result"] = ev.get("result") if isinstance(ev.get("result"), str) else ""
            out["result_error"] = bool(ev.get("is_error"))
            continue
        content = (ev.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                names[block.get("id", "")] = block.get("name", "")
                if block.get("name", "").endswith("send_message"):
                    out["send_attempts"] += 1
            elif block.get("type") == "tool_result":
                name = names.get(block.get("tool_use_id", ""), "")
                if name.endswith("send_message") and not block.get("is_error"):
                    out["send_ok"] += 1
                elif name.endswith("get_pending_action_memories") and not block.get("is_error"):
                    try:
                        results = json.loads(_text(block.get("content"))).get("results", [])
                    except (ValueError, AttributeError):
                        continue
                    out["pending_ids"] += [r["id"] for r in results if isinstance(r, dict) and r.get("id")]
    return out


def run(dry_run: bool = False, runner=subprocess.run, db_path=None) -> int:
    """Returns the number of memos marked as mailed. Raises SuggestError when the run did not complete."""
    db.init_db(db_path)
    if not db.pending_memories(limit=1, db_path=db_path, exclude_suggested=True):
        log.info("no new memos to suggest; Claude not called")
        return 0
    cmd = [claude_bin(), "-p", PROMPT + (DRY_RUN_PROMPT if dry_run else ""),
           "--output-format", "stream-json", "--verbose", "--allowedTools", ALLOWED_TOOLS]
    try:
        r = runner(cmd, capture_output=True, text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL,
                   cwd=db.PROJECT_ROOT)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SuggestError(f"claude failed: {type(e).__name__}") from e
    if r.returncode != 0:
        raise SuggestError(f"claude exited {r.returncode}: {(r.stderr or '')[-300:]}")
    info = parse_run(r.stdout)
    if info["result"] is None or info["result_error"]:
        raise SuggestError("claude returned no successful result")
    if dry_run:
        print(info["result"])
        return 0
    sent = info["send_ok"] > 0
    nothing_mailable = (info["send_attempts"] == 0 and info["pending_ids"]
                        and info["result"].rstrip().endswith("RESULT: none"))
    if not (sent or nothing_mailable):
        raise SuggestError(f"mail not confirmed (send attempts: {info['send_attempts']}, ok: {info['send_ok']})")
    marked = db.mark_suggested(info["pending_ids"], db_path)
    log.info("%s; marked %d memos", "mail sent" if sent else "nothing mailable", marked)
    return marked


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="voice-memory-suggest", description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="print the mail body instead of sending; mark nothing")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        run(dry_run=args.dry_run)
    except SuggestError as e:
        log.error("%s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
