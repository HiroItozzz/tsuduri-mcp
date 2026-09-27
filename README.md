# tsuduri-mcp

claude.ai の会話履歴を SQLite に取り込み、MCP クライアント（Claude Code など）から扱えるようにする MCP サーバー。

## 会話の取り込み

claude.ai の「データのエクスポート」で手に入る `conversations-000.zip`（または展開した `conversations.json`）を渡す。

```bash
uv run tsuduri-import path/to/conversations-000.zip
```

- 取り込み先は、ユーザーのデータ置き場の `tsuduri.db`（Linux は `~/.local/share/tsuduri-mcp/`、Windows は `%LOCALAPPDATA%\tsuduri-mcp\`）。環境変数 `TSUDURI_DB` か `--db` で変えられる
- ログは同じ場所の `logs/tsuduri.log`（`TSUDURI_LOG` で変えられる）
- 何度取り込んでも重複しない。新しいエクスポートを取り込むと、続きのある会話が更新される

## MCP サーバーの登録

Claude Code:

```bash
claude mcp add tsuduri -- uv run --directory /path/to/tsuduri-mcp --env-file /path/to/tsuduri-mcp/.env tsuduri-mcp
```

Windows（PowerShell）では、パスを Windows の形で書く。

```powershell
claude mcp add tsuduri -- uv run --directory C:\path\to\tsuduri-mcp --env-file C:\path\to\tsuduri-mcp\.env tsuduri-mcp
```

Claude デスクトップ（`claude_desktop_config.json`）:

```json
{
  "mcpServers": {
    "tsuduri": {
      "command": "uv",
      "args": ["run", "--directory", "C:\\path\\to\\tsuduri-mcp", "--env-file", "C:\\path\\to\\tsuduri-mcp\\.env", "tsuduri-mcp"]
    }
  }
}
```

`.env` には `GEMINI_API_KEY` と、はてなの `HATENA_ENTRY_URL`・`HATENA_CONSUMER_KEY`・`HATENA_CONSUMER_SECRET`・`HATENA_ACCESS_TOKEN`・`HATENA_ACCESS_TOKEN_SECRET` を書く。

## ツール

| ツール | 用途 |
|---|---|
| `search_messages` | キーワード検索（部分一致。日本語可） |
| `list_conversations` | 期間・タイトルで会話の一覧 |
| `get_messages` | 会話の一部を読む |
| `export_conversation` | 会話の全文を Markdown ファイルに書き出す |
| `summarize_conversation` | Gemini で要約する（要約は保存して使い回す） |
| `draft_blog_post` | Gemini でブログの下書きを作る（投稿はしない） |
| `post_blog_article` | はてなブログへ投稿する（既定は下書き）。投稿した範囲を自動で記録する |
| `record_blog_post` | `post_blog_article` を使わずに投稿したとき、範囲を記録する（投稿自体はしない） |
| `check_blog_posts` | 記録した投稿の、はてな側での今の状態を確かめる |
| `publish_blog_post` | 記録した記事を公開する（既定は1分後の予約。`confirm=true` で実行） |
| `unpublish_blog_post` | 記録した記事を下書きに戻す（予約中なら予約を取り消す。`confirm=true` で実行） |

prompt として `summarize_with_claude` と `draft_blog_with_claude` もある（クライアント自身が要約・下書きをする）。

要約と下書きには環境変数 `GEMINI_API_KEY` が必要。

## 開発

```bash
uv run ruff check . && uv run ruff format . && uv run ty check && uv run pytest
```

設計の判断は [docs/design.md](docs/design.md) にまとめている。
