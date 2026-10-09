import argparse
from pathlib import Path

from .sources import ChatGptExportSource, ClaudeExportSource
from .store import ConversationStore, connect, default_db_path


def main() -> None:
    parser = argparse.ArgumentParser(description="claude.ai / ChatGPT のエクスポートを SQLite に取り込む")
    parser.add_argument("path", type=Path, help="conversations.json、またはそれを含む zip")
    parser.add_argument(
        "--format", choices=["claude", "chatgpt"], default="claude", help="エクスポートの形式（既定: claude）"
    )
    parser.add_argument("--db", type=Path, default=None, help="取り込み先（既定: $TSUDURI_DB かデータ置き場）")
    args = parser.parse_args()

    db_path = args.db or default_db_path()
    conn = connect(db_path)
    try:
        source = ChatGptExportSource(args.path) if args.format == "chatgpt" else ClaudeExportSource(args.path)
        result = ConversationStore(conn).import_conversations(source.load())
    finally:
        conn.close()

    print(
        f"{db_path} に取り込みました: 新規 {result.added} 件 / 更新 {result.updated} 件 / "
        f"変化なし {result.unchanged} 件 / 追加メッセージ {result.messages_added} 件 / "
        f"消えた本文・メッセージの印 {result.notes_added} 件"
    )
