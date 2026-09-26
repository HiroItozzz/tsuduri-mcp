# 設計メモ

決めたことと、その理由を残す。あとで見直すときに「なぜこうしたか」がわかるようにする。

## 会話データの取り込み（SQLite）

### 元データ

claude.ai の公式エクスポート（設定 → データのエクスポート）。manifest の JSON に zip の URL が4つ並んでおり、各 URL は1回しか使えない。

| zip | 中身 | 扱い |
|---|---|---|
| conversations | `conversations.json`（全会話） | 取り込む |
| projects | プロジェクトごとの JSON（名前・指示・ナレッジ） | 後回し（下記） |
| memories | Claude のメモリ | 取り込まない |
| light_metadata | ログイン履歴（IP・位置）とメールアドレス | 取り込まない（個人情報で、使い道がない） |

2026-09-26 のエクスポート: 1,157 会話・41,152 メッセージ、最古は 2025-05-17。展開したものは `assets/export-2026-09-26/` に置いている（`assets/` は git 管理外）。

### 取り込みの規則

- **会話**: uuid が主キー。`updated_at` が保存済みより新しいときだけ、名前・要約・`updated_at` を上書きする。古いエクスポートを後から取り込んでも巻き戻らない
- **メッセージ**: uuid が主キー。`INSERT OR IGNORE` で、すでにあるものは上書きしない
  - 旧エクスポート（2026-09-20 まで）と新エクスポートを比べると、メッセージの `updated_at` は1件も変わっていないのに、122 件で content が変わっていた。ほとんどは tool_result の中身の違いだったが、4 件は新しいほうで content が空になっていた
  - 上書きしない方針なら、エクスポートから消えた本文も DB に残る。実際に旧→新の順で取り込んだ DB で、この 4 件の本文が残っていることを確認した
- 日時はすべて `2026-01-01T00:00:00.000000Z` の形にそろっているので、文字列のまま比べている
- 1回の取り込みは1つのトランザクション。途中で失敗したら何も書き込まれない

### 本文（`messages.text`）

- content の **text ブロックだけ**を `\n\n` でつないで作る。検索と要約はこの列を使う
- message の `text` 欄は使わない。画面表示用の定型文（`This block is not supported on your current device yet.` など）が混ざっているため
- thinking / tool_use / tool_result / injected_prompt_block / token_budget は本文に入れない。ただし `raw_content` 列に元のブロックを JSON のまま全部残しているので、あとで方針を変えても再取り込みなしで作り直せる
- text ブロックがないメッセージ（添付だけ、ツール呼び出しだけなど）は本文が空になる。新エクスポートでは 4,448 件

### テーブル

- `conversations(uuid, name, summary, created_at, updated_at)`
- `messages(uuid, conversation_uuid, position, parent_uuid, sender, text, created_at, updated_at, raw_content, attachments, files)`
  - `position` はエクスポート内での並び順。配列の順は時刻順と一致していた
  - `parent_uuid` は親メッセージ。先頭メッセージの親として入っている `00000000-0000-4000-8000-000000000000` は NULL にしている。407 会話で枝分かれ（編集や再生成）があり、エクスポートには全部の枝が入っている。いま表示されている枝がどれかは、エクスポートからはわからない
  - `attachments`（`extracted_content` に本文あり）と `files`（名前と uuid だけ）は JSON のまま保存している
- 会話とプロジェクトの対応はエクスポートに含まれていない。`projects` テーブルは、会話とは独立したものとして後で足す

### DB の場所

- 既定は `data/tsuduri.db`（cwd 基準の相対パス）。環境変数 `TSUDURI_DB` で変えられる
- MCP サーバーは `uv run --directory <このリポジトリ>` で起動するので、cwd はいつもリポジトリのルートになる
- `*.db` などの SQLite ファイルは git 管理外

### 構成

- `models.py`: `Conversation` / `Message`（dataclass）。形式に依存しない
- `sources.py`: `ConversationSource`（読み込みのインターフェース）と、その実装の `ClaudeExportSource`。claude.ai 形式の知識（キー名、ブロックの種類、ルートの親 uuid）はここに閉じ込める。`conversations.json` と、それを含む zip のどちらも読める
- `store.py`: `ConversationStore`。SQLite への書き込みと読み出し
- `importer.py`: `tsuduri-import` コマンド

## 未決定

- 検索の方法（`LIKE` か FTS5 か）。ツールを作るときに決める
- 枝分かれした会話を、ツールでどう見せるか
- 定期的な取り込みの方法
- projects テーブル
