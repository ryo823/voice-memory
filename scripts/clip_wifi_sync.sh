#!/bin/bash
# Join ClipAP on wlan0 only, download via UDP, then ALWAYS restore the original Wi-Fi profile.
SDK=$HOME/clip-sdk; ORIG=preconfigured; MAC=C3:31:EB:7C:F4:8D; SESSION=$1
restore() {
  sudo -n nmcli con delete clipap >/dev/null 2>&1
  sudo -n nmcli con up "$ORIG" ifname wlan0 >/dev/null 2>&1
  echo "restored: $(nmcli -t -f GENERAL.CONNECTION dev show wlan0)"
}
trap restore EXIT
cd $SDK/src
OUT=$($SDK/venv/bin/python tools/clip-cli.py --device $MAC --timeout 30 wifi on 2>&1) || { echo "wifi on failed: $OUT"; exit 1; }
SSID=$(grep -oE 'ClipAP_[0-9A-Za-z]+' <<<"$OUT" | head -1); PW=$(awk '/Password:/{print $2}' <<<"$OUT")
echo "AP: $SSID"
sudo -n nmcli con add type wifi ifname wlan0 con-name clipap ssid "$SSID" autoconnect no \
  wifi.hidden yes wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PW" ipv4.never-default yes ipv4.method auto >/dev/null || exit 2
sudo -n nmcli con up clipap ifname wlan0 || exit 3
sleep 3; ip -br a show wlan0
timeout 150 $SDK/venv/bin/python tools/clip-cli.py --transport wifi -o $SDK/recordings sync --session "$SESSION" 2>&1 | tr '\r' '\n' | tail -15
