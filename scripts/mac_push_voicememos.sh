#!/bin/bash
# Run on the Mac: copy iCloud-synced Voice Memos to the Pi's inbox (voice-memory-inbox ingests them there).
# Usage: mac_push_voicememos.sh <user>@<pi-host>   (the terminal needs Full Disk Access to read the folder)
set -euo pipefail
DEST="${1:?usage: $0 <user>@<pi-host>}"
SRC="$HOME/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings/"
[ -d "$SRC" ] || SRC="$HOME/Library/Application Support/com.apple.voicememos/Recordings/"
[ -d "$SRC" ] || { echo "Voice Memos folder not found (Full Disk Access?)" >&2; exit 1; }
# Only copies; never --delete, so nothing on either side is removed. macOS rsync is old: no --append-verify etc.
rsync -rt --ignore-existing --include='*.m4a' --exclude='*' "$SRC" "$DEST:voice-memory/data/inbox/voicememos/"
