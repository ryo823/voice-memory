# 会話ごとのメモ分割

録音長が `VOICE_MEMORY_SPLIT_MIN_DURATION_SEC`(既定600秒)以上のファイルは、話の途切れ目で複数メモに分けて登録する。

## 分割ルール(`ingest/split.py`)
境界は Whisper の区間ではなく **VAD の発話区間の隙間** で決める。Whisper の区間は連続していて無音を飲み込むため、隙間が取れない。
1. 発話区間が `GAP_SEC`(45)秒以上空いたら切る。
2. 1チャンクが `MAX_CHUNK_SEC`(900)秒を超えたら、先頭から900秒以内で `MAX_SPLIT_GAP_SEC`(3)秒以上空いた最大の隙間で切る。なければ900秒に最も近い区間境界で切る。
3. 各 Whisper 区間は、その区間の発話が最も多く重なるチャンクに入れる(全区間がちょうど1チャンクに入る)。
4. `MIN_CHUNK_SEC`(30)秒未満、または `MIN_CHUNK_CHARS`(50)字未満のチャンクは前のチャンクへ、先頭なら次へ結合する。どちらか一方でも下回れば結合する。
5. 結果が1チャンクなら分割しない(従来どおり1メモ)。

設定は `VOICE_MEMORY_SPLIT_*` 環境変数、`VOICE_MEMORY_SPLIT=0` で無効化。

## DB(追加のみ・冪等。`database.migrate`)
- `memories`: `audio_start_sec` `audio_end_sec` `parent_memory_id` `split_method` `archived`(既定0)
- `ingested_file_memories(file_sha256, memory_id PK, segment_index)` と `UNIQUE(file_sha256, segment_index)`
- `ingested_files.segments_json`: 時刻付き文字起こしのキャッシュ(再試行で再文字起こししない)

## 取り込みの原子性
1ファイルの全チャンクのメモ・中間表・`done` 印を1トランザクションで書く。要約が途中で失敗すると何も残らず、`transcribed` 状態から再試行できる。

## 検索・一覧
`archived=1` は `search_memories` / `recent_memories` / `memories_by_tag` / `pending_memories` / `list_tags` から除外される。MCP の `search_voice_memories` は `include_archived=true` で含められる。`get_voice_memory` はID指定なら読める。

## 削除
子メモを1件消してもファイルは取り込み済みのまま。最後の1件を消したときだけ墓標(`deleted`)にする。親メモを消すと子の `parent_memory_id` は NULL になる。
