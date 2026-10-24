# Clip 録音再開の実機検証手順

自動テストでは検証できない項目。結果は実施日とともにここに追記する。
共通: 事前に `journalctl --user -u voice-memory-clip-sync -n 50` と `logs/ingest.log` を確認。Clip内の録音は削除しないこと。

| # | 操作 | 期待結果 |
|---|------|----------|
| 1 | Clipを充電器から外し、録音せず(IDLE)で次のtimer実行を待つ | 新規セッションがあれば取得・登録後に `resumed: true`。Clipが録音状態(LED)に戻る |
| 2 | 取得後にClipが `WIFI_SYNC` のまま残る場合 | その間は「Clip is WIFI_SYNC」で見送り。IDLEに戻った次の実行で録音が再開する(空白時間を記録) |
| 3 | 取得中にClipの電源を切る/圏外へ出す | `fetch` 失敗、state は `failed`(attempts+1)。`nmcli con show` に `clipap` が残らず、wlan0 が `preconfigured` |
| 4 | 取得中に `systemctl --user stop voice-memory-clip-sync` | wlan0 が復旧する(SIGTERM でtrap動作) |
| 5 | 取得中に `kill -9` (スクリプト本体) し、`clipap` を残す | 次のtimer実行の冒頭で `stale clipap profile found` と出て復旧 |
| 6 | Piを再起動(timerは `OnBootSec=3min`) | 再起動後にtimerが再開。IDLEかつ非充電のClipは録音再開 |
| 7 | 充電器に載せて録音中 | `AT+STOP` で停止→取得。載せている間は再開しない |

確認コマンド: `nmcli -t -f NAME,DEVICE con show --active` / `python scripts/clip_control.py C3:31:EB:7C:F4:8D state`
