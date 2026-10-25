#!/usr/bin/env bash
# Scheduled, proposal-only run of the suggest-actions skill (see systemd/voice-memory-suggest.*).
# Mails a candidate list to the owner; never writes to Calendar or records registrations.
# Usage: scripts/suggest_actions_scheduled.sh [--dry-run]   (--dry-run prints instead of mailing)
set -euo pipefail
cd "$(dirname "$0")/.."

prompt="suggest-actions スキルを提案専用モードで実行して。"
if [ "${1:-}" = "--dry-run" ]; then
  prompt="$prompt メールは送らず、本文を標準出力に出すだけにして。"
fi

# Only read tools + Gmail send are allowed: a memo's text cannot make this run touch Calendar or the DB.
exec "$HOME/.local/bin/claude" -p "$prompt" \
  --allowedTools "mcp__voice-memory__get_pending_action_memories,mcp__voice-memory__get_voice_memory,mcp__claude_ai_Gmail__send_message" \
  < /dev/null
