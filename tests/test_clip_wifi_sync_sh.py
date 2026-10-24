"""clip_wifi_sync.sh against fake nmcli/sudo/clip-cli: failures must be reported and Wi-Fi always restored."""
import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "clip_wifi_sync.sh"


def _exe(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def box(tmp_path):
    bin_, sdk = tmp_path / "bin", tmp_path / "sdk"
    cons, log = tmp_path / "cons", tmp_path / "log"
    cons.write_text("preconfigured\n")
    log.write_text("")
    _exe(bin_ / "sudo", 'shift; [ "$1" = -n ] && shift; exec "$@"\n')
    _exe(bin_ / "ip", "echo wlan0 UP\n")
    _exe(bin_ / "nmcli", r'''
echo "nmcli $*" >> "$LOG"
case "$*" in
  *"con show"*) cat "$CONS" ;;
  *"con delete "*) sed -i "/^${3}\$/d" "$CONS" ;;
  *"con add "*) echo clipap >> "$CONS" ;;
  *"con up "*) [ "$3" = "${NM_UP_FAIL:-}" ] && exit 4 ;;
  *"dev show"*) echo "GENERAL.CONNECTION:preconfigured" ;;
esac
exit 0
''')
    _exe(sdk / "venv/bin/python", r'''
case "$*" in
  *"wifi on"*) [ "${FAKE_WIFION:-ok}" = fail ] && { echo nope; exit 1; }
               echo "SSID: ClipAP_AB12"; echo "Password: pw123"; exit 0 ;;
  *sync*) echo "partial..."; case "${FAKE_SYNC:-ok}" in ok) echo Done; exit 0;; timeout) exit 124;; *) echo Failed; exit 1;; esac ;;
esac
''')
    (sdk / "src/tools").mkdir(parents=True)

    def run(**env):
        e = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "SDK": str(sdk), "LOG": str(log),
             "CONS": str(cons), "SETTLE": "0", "ORIG": "preconfigured", **env}
        p = subprocess.run([str(SCRIPT), "20261024115101"], capture_output=True, text=True, env=e, timeout=30)
        return p, log.read_text().splitlines(), cons.read_text().split()

    return run, cons


def test_success_restores_original(box):
    run, _ = box
    p, log, cons = run()
    assert p.returncode == 0 and "Done" in p.stdout
    assert cons == ["preconfigured"]  # clipap removed
    assert "nmcli con up preconfigured ifname wlan0" in log


@pytest.mark.parametrize("mode,code", [("fail", 1), ("timeout", 124)])
def test_sync_failure_propagates_through_pipe(box, mode, code):
    run, _ = box
    p, log, cons = run(FAKE_SYNC=mode)
    assert p.returncode == code  # not tail's 0
    assert cons == ["preconfigured"] and "nmcli con up preconfigured ifname wlan0" in log


def test_wifi_on_failure_never_touches_nm_but_restore_is_harmless(box):
    run, _ = box
    p, log, cons = run(FAKE_WIFION="fail")
    assert p.returncode == 1
    assert not any("con add" in line for line in log) and cons == ["preconfigured"]


def test_join_failure_restores(box):
    run, _ = box
    p, log, cons = run(NM_UP_FAIL="clipap")
    assert p.returncode == 3
    assert cons == ["preconfigured"] and "nmcli con up preconfigured ifname wlan0" in log


def test_restore_failure_is_reported(box):
    run, _ = box
    p, _, _ = run(NM_UP_FAIL="preconfigured")
    assert p.returncode == 4 and "restore FAILED" in p.stderr
    p, _, _ = run(NM_UP_FAIL="preconfigured", FAKE_SYNC="fail")
    assert p.returncode == 1  # the original failure code is kept


def test_stale_clipap_is_cleaned_at_start_and_only_clipap(box):
    run, cons_file = box
    cons_file.write_text("preconfigured\nclipap\n有線接続 1\n")
    p, log, cons = run(FAKE_WIFION="fail")
    assert "stale clipap" in p.stdout
    assert "nmcli con delete clipap" in log and "nmcli con up preconfigured ifname wlan0" in log
    assert cons == ["preconfigured", "有線接続", "1"]  # others untouched (split on whitespace)
    assert not any("con delete" in line and "clipap" not in line for line in log)


def test_no_stale_profile_means_no_restore_at_start(box):
    run, _ = box
    p, log, _ = run(FAKE_WIFION="fail")
    assert "stale" not in p.stdout
