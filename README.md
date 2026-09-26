# tsuduri-mcp

claude.ai の会話履歴を SQLite に取り込み、MCP クライアント（Claude Code など）から扱えるようにする MCP サーバー。

## 会話の取り込み

claude.ai の「データのエクスポート」で手に入る `conversations-000.zip`（または展開した `conversations.json`）を渡す。

```bash
uv run tsuduri-import path/to/conversations-000.zip
```

- 取り込み先は `data/tsuduri.db`。環境変数 `TSUDURI_DB` か `--db` で変えられる
- 何度取り込んでも重複しない。新しいエクスポートを取り込むと、続きのある会話が更新される

## MCP サーバーの登録

```bash
claude mcp add tsuduri -- uv run --directory /path/to/tsuduri-mcp tsuduri-mcp
```

## 開発

```bash
uv run pytest
```

設計の判断は [docs/design.md](docs/design.md) にまとめている。
