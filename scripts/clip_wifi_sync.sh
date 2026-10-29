#!/bin/bash
# Join ClipAP on wlan0 only, download via UDP, then ALWAYS restore the original Wi-Fi profile.
# Exit: 0 ok | 1 wifi-on failed | 2/3 could not set up/join ClipAP | 4 restore failed | else: sync's own status.
# SDK/ORIG/MAC are overridable for tests; the defaults are the production values.
set -o pipefail
SDK=${SDK:-$HOME/clip-sdk}; ORIG=${ORIG:-preconfigured}; MAC=${MAC:-C3:31:EB:7C:F4:8D}; SESSION=$1
AP_CON=clipap
restore() {
  local rc=$?
  sudo -n nmcli con delete "$AP_CON" >/dev/null 2>&1
  if ! sudo -n nmcli con up "$ORIG" ifname wlan0 >/dev/null 2>&1; then
    echo "restore FAILED: could not bring up '$ORIG'" >&2
    exit $((rc == 0 ? 4 : rc))
  fi
  echo "restored: $(nmcli -t -f GENERAL.CONNECTION dev show wlan0)"
}
trap restore EXIT
# A previous run killed before its trap ran (SIGKILL, power loss) leaves the clipap profile behind:
# restore the normal connection first. Only the clipap profile is ever deleted.
if nmcli -t -f NAME con show 2>/dev/null | grep -qx "$AP_CON"; then
  echo "stale $AP_CON profile found; restoring $ORIG"
  restore
fi
cd "$SDK/src" || exit 1
OUT=$("$SDK/venv/bin/python" tools/clip-cli.py --device "$MAC" --timeout 30 wifi on 2>&1) || { echo "wifi on failed: $OUT"; exit 1; }
SSID=$(grep -oE 'ClipAP_[0-9A-Za-z]+' <<<"$OUT" | head -1); PW=$(awk '/Password:/{print $2}' <<<"$OUT")
echo "AP: $SSID"
sudo -n nmcli con add type wifi ifname wlan0 con-name "$AP_CON" ssid "$SSID" autoconnect no \
  wifi.hidden yes wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PW" ipv4.never-default yes ipv4.method auto >/dev/null || exit 2
sudo -n nmcli con up "$AP_CON" ifname wlan0 || exit 3
sleep "${SETTLE:-3}"; ip -br a show wlan0
# pipefail: the exit status is that of `timeout ... clip-cli sync` (124 on timeout), not of tail
timeout 1500 "$SDK/venv/bin/python" tools/clip-cli.py --transport wifi -o "$SDK/recordings" sync --session "$SESSION" 2>&1 | tr '\r' '\n' | tail -15
