# FTS5 実機DB検証手順（Pi）

目的: 本番DB（`data/voice_memory.db`）で、memories と FTS 索引の件数・検索結果が一致していることを確認する。
**この手順は読み取りとバックアップのみ。** DBを書き換える操作（手順5の再構築）は、ずれが見つかった場合に限り、実行前に合意を取る。

注意: `voice-memory-inbox` / `voice-memory-clip-sync` のtimerが動いていると件数が途中で増える。
比較は手順2の1回のクエリ（同一スナップショット）で行う。厳密に見たいときは先に
`systemctl --user stop voice-memory-inbox.timer`（終わったら `start`）。処理中のサービスは止めない。

## 1. バックアップ（Pi）
```bash
cd ~/voice-memory
.venv/bin/voice-memory-backup --verify      # DBスナップショット作成→復元確認（既存データは変更・削除しない）
ls -lt ~/backups/voice-memory/ | head -3    # 最新の voice_memory-*.db を確認
```

## 2. 件数の比較（Pi・読み取りのみ）
```bash
.venv/bin/python -I - <<'PY'
import sqlite3
c = sqlite3.connect("file:data/voice_memory.db?mode=ro", uri=True)
m, f = c.execute("SELECT (SELECT COUNT(*) FROM memories), (SELECT COUNT(*) FROM memories_fts)").fetchone()
print("memories:", m, "fts:", f, "OK" if m == f else "MISMATCH")
# 件数が同じでも内容がずれていないか（件数比較では検出できない種類のずれ）
bad = c.execute("""SELECT COUNT(*) FROM memories m JOIN memories_fts x ON x.memory_id = m.id
  WHERE x.title IS NOT m.title OR x.summary IS NOT m.summary OR x.transcript IS NOT m.transcript""").fetchone()[0]
orphan = c.execute("SELECT COUNT(*) FROM memories_fts WHERE memory_id NOT IN (SELECT id FROM memories)").fetchone()[0]
print("content mismatch:", bad, "orphan fts rows:", orphan)
PY
```
期待: `OK`、`content mismatch: 0`、`orphan fts rows: 0`。

## 3. 検索結果の比較（Pi・読み取りのみ）
FTS経由（3文字以上）とLIKE経由の結果が同じ件数になることを、実在する語で確認する。
```bash
.venv/bin/python -I - <<'PY'
import sqlite3
c = sqlite3.connect("file:data/voice_memory.db?mode=ro", uri=True)
# 文字起こしから3文字の語を拾って比較（語は毎回変わってよい）
t = c.execute("SELECT transcript FROM memories ORDER BY created_at DESC LIMIT 1").fetchone()[0]
terms = {w for i in range(0, max(len(t) - 3, 0), max(len(t) // 4, 1)) if len(w := t[i:i + 3]) == 3 and w.strip()}
for term in terms:
    fts = c.execute("SELECT COUNT(*) FROM memories WHERE id IN (SELECT memory_id FROM memories_fts WHERE memories_fts MATCH ?)",
                    ('"' + term.replace('"', '""') + '"',)).fetchone()[0]
    like = c.execute("SELECT COUNT(*) FROM memories m WHERE instr(m.title||m.summary||m.transcript, ?) > 0 OR m.id IN "
                     "(SELECT mt.memory_id FROM memory_tags mt JOIN tags t ON t.id=mt.tag_id WHERE instr(t.name, ?) > 0)",
                     (term, term)).fetchone()[0]
    print(repr(term), "fts:", fts, "like:", like, "OK" if fts == like else "DIFF")
PY
```
期待: すべて `OK`。さらに Claude Code から `search_voice_memories` を実語（3文字以上／2文字以下／空白区切りAND）で実行し、結果が妥当か目視する。

## 4. 判定
- 全て期待どおり → 完了。結果をこのファイル末尾に日付付きで追記する。
- `MISMATCH` / `DIFF` / `content mismatch` があれば → 手順5へ（先に内容を共有する）。

## 5. ずれがあった場合のみ: 索引の再構築（DBを変更する・要合意）
索引（memories_fts）だけを作り直す。memories / tags / 録音は変更しない。手順1のバックアップが済んでいること、他のプロセスが書き込み中でないこと（timer停止）を確認してから:
```bash
.venv/bin/python -I -c "
from contextlib import closing
from voice_memory import database as db
with closing(db.connect()) as c: print('indexed:', db.rebuild_fts(c))"
```
再実行して手順2・3が全て期待どおりになることを確認する。

## 既知の限界
- `ensure_fts()`（`voice-memory-init`・ingest時に自動実行）は件数しか比べない。同数のずれは手順2の内容比較か手動の `rebuild_fts()` で直す。通常経路（`save_memory`）はmemoriesとFTSを同一トランザクションで更新するのでずれない。
- タグだけをDB外から直接変更した場合の索引ずれは、手順2では検出しない（tags列は比較対象外）。
