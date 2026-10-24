# Privacy policy / プライバシーポリシー

voice-memory-backup is a personal, single-user backup tool (rclone) for the voice-memory project.
It is used only by its owner and is not offered to other users.

- It requests only the Google Drive scope `drive.file`, and only reads/writes files it created itself in the owner's own Google Drive.
- It copies the owner's own backups (a SQLite database and audio recordings) to that Drive. It does not read any other Drive content.
- No data is shared with, sold to, or used by anyone else. The tool has no server and no analytics; the OAuth tokens stay on the owner's own device.

本ツールは voice-memory の個人用バックアップ(rclone)で、所有者本人のみが使用します。
Google Drive のスコープは `drive.file` のみで、本ツールが作成したファイルだけを本人の Drive に書き込みます。
取得したデータを第三者に提供・販売・利用することはありません。トークンは本人の端末内にのみ保存されます。

Contact: ryosuke.hirayama@gmail.com

## Calendar / Tasks suggestions (phase 1)

Approved events are created in the owner's own Google Calendar through the owner's claude.ai Google Calendar connector, from a Claude Code session the owner is driving. Only the event title, time, and a short description (memory ID and summary) are written; transcripts are never sent. This repository holds no Google credentials for it.

承認した予定は、本人の claude.ai の Google Calendar コネクタ経由で、本人の Calendar にだけ作成します。書き込むのは予定名・日時・説明（メモIDと要約）のみで、文字起こし全文は送りません。
