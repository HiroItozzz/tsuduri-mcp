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
- `messages(id, uuid, conversation_uuid, position, parent_uuid, sender, text, created_at, updated_at, raw_content, attachments, files)`
  - `id` は整数の主キーで、全文検索の索引（`messages_fts`）が参照する。uuid を主キーにした表の暗黙の rowid は VACUUM で変わることがあるため、明示した
  - `position` はエクスポート内での並び順（0 始まり）。ツールでは「index」と呼ぶ。配列の順は時刻順と一致していた
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
- `dates.py`: ツールの引数（ローカル時刻）と DB（UTC）の日時の変換
- `render.py`: ツールの戻り値のテキストを組み立てる
- `server.py`: MCP ツールの定義
- `importer.py`: `tsuduri-import` コマンド

SQLAlchemy は使わない。中心の FTS5（仮想テーブル、`MATCH`、トリガー）は SQLAlchemy でも生の SQL になり、表が2つで書き込みも取り込みの1か所だけなので、ORM の利点が小さいため。動的な WHERE は小さな `_Where` で組み立てている。

## 検索

- FTS5 の `trigram` トークナイザで、本文の部分一致を索引で引く。日本語でも分かち書きなしで使え、大文字・小文字は区別しない
- trigram は3文字以上でしか引けないので、2文字以下のキーワードは `LIKE` で探す（本物の DB で 0.6 秒ほど）
- キーワードはフレーズとして渡すので、`AND` や `*` などの FTS の演算子は文字として扱われる。LIKE の `%` `_` もエスケープする
- 取り込みは索引づくりのぶん遅くなった（新エクスポート全件で約 7 秒 → 約 42 秒）。たまにしか実行しないので許容している
- メッセージは追加だけなので、索引の更新は INSERT のトリガーだけにしている

## MCP ツール

AI のコンテキストを節約することを優先している。

| ツール | 用途 |
|---|---|
| `search_messages` | キーワード検索。all/any、除外語、発言者、期間、会話で絞り込み、ページ分け |
| `list_conversations` | 期間・タイトルで会話の一覧（例: 先週の会話） |
| `get_messages` | 会話の index の範囲だけを読む |
| `export_conversation` | 全文を Markdown ファイルに書き出して、パスだけを返す |

- **戻り値はテキスト**（`structured_output=False`）。構造化出力にすると、同じ内容がインデントつき JSON の文字列と構造化データの両方で送られるため。キー名のくり返しや改行のエスケープもなくなる
- 検索結果は1件ごとに `conversation=<uuid> index=<n>`、発言者、日時、タイトル、本文。長い本文は、最初に当たったキーワードのまわりを `max_chars` 文字だけ切り出し、何文字目かを添える。続きは `get_messages` で読む
- 件数の多い結果は「N 件中 a〜b 件目（続きは offset=…）」と書き、AI が続きを取りに行けるようにする
- 一覧の期間は「期間中にやりとりのあった会話」（作成が期間の終わりより前、かつ最終更新が期間の始まり以降）。タイトルが空の会話（新エクスポートで 219 件）は、最初の発言の冒頭を添える
- 日付の引数はローカル時刻の `YYYY-MM-DD` か ISO 8601。`until` に日付だけを渡すと、その日を含む。表示もローカル時刻
- 枝分かれした会話は全部の枝を index 順に並べ、直前以外のメッセージへの返信に「分岐」と表示する
- `export_conversation` は、AI がファイルを読めるクライアント（Claude Code など）向け。書き出し先は一時ディレクトリの `tsuduri-mcp/<uuid>.md`。uuid は DB に存在することを確かめてからファイル名に使う
- DB がないときは、取り込みのコマンドを案内するエラーにする
- サーバーの `instructions` に、ツールの使い分けを書いている

## 開発ツール

ruff（リント・整形）と ty（型チェック）。`uv run ruff check . && uv run ruff format . && uv run ty check && uv run pytest`

## 未決定

- 要約（Gemini / DeepSeek）と投稿のツール。cha2hatena の `llm/` と `blog/` を移す
- 定期的な取り込みの方法
- projects テーブル
- thinking やツールの入出力も検索対象にするか（`raw_content` に残っている）
