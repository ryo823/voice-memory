# Google Drive へのバックアップ設定（rclone）

`voice-memory-backup --rclone-remote gdrive-vm:voice-memory-backup` を動かすための、初回セットアップと詰まりどころ。
バックアップ本体の使い方は README「バックアップ」を参照。

## 構成
- 保存先: Google Drive（自分専用・暗号化なし）。rclone のリモート名は `gdrive-vm`、scope は `drive.file`（rclone が作ったファイルだけ触れる）。
- OAuth クライアントは **専用の GCP プロジェクト**（`voice-memory-backup-<日付>`）で作る。rclone 共有の client_id は2026年中に廃止予定。
- **rclone の設定は Pi にだけ置く。** トークン発行（`rclone authorize`）だけ、ブラウザのある PC で行う。

## 手順
1. GCP プロジェクトを作り Drive API を有効化: `gcloud projects create ...` / `gcloud services enable drive.googleapis.com --project ...`
2. 同意画面（Google Auth Platform）: ユーザータイプ「外部」、アプリ名・サポートメール・連絡先メールを入力、スコープは追加しない。
3. **公開ステータスを「本番環境」にする。** 「テスト」のままだとリフレッシュトークンが7日で失効し、自動バックアップが1週間後に止まる。
   - 本番にするには **ホームページ URL とプライバシーポリシー URL が必須**。
   - `github.com` は承認済みドメインに追加できない（所有確認ができない）。
   - 対処: 同じプロジェクトで Firebase Hosting を有効化（`firebase projects:addfirebase <id>`）し、`https://<id>.web.app` に簡単なトップとプライバシーポリシーのページを置く。Search Console で「URL プレフィックス」の所有確認（HTML タグ）をしてから、承認済みドメインに `<id>.web.app` を追加する。
4. クライアントを「デスクトップアプリ」で作成し、ID とシークレット（JSON）を控える。JSON はリポジトリに置かない（`client_secret_*.json` は .gitignore 済み）。設定後は不要。
5. **Pi で** `rclone config`: 新規 `gdrive-vm`、種類 `drive`、client_id/secret を入力、scope `drive.file`、`service_account_file` は空、advanced は n、**auto config は n**。
6. 表示された `rclone authorize "drive" "eyJ..."` を **PC で**そのまま実行し（引数なしだと rclone 共有の client_id で発行されて合わない）、出たトークンを Pi の `config_token>` に貼る。Shared Drive は n。
7. 確認: `rclone lsd gdrive-vm:` と `voice-memory-backup --rclone-remote gdrive-vm:voice-memory-backup`。

## 詰まりどころ
- `address already in use` (53682): VS Code のポート転送が Mac 側で 53682 を握っている。「ポート」タブで転送を停止する。
- 設定が Pi に無い: `rclone config` を PC で実行したのが原因。`rclone config file` で保存先を確認する。
- 認証が失効した場合: Pi で `rclone config reconnect gdrive-vm:`（トークンだけ PC で再発行）。
- Drive の中身は rclone が作ったファイルのみ見える（`drive.file`）。ブラウザで手動作成したフォルダは rclone から見えない。
