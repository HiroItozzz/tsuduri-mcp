import argparse
from pathlib import Path

from .sources import ClaudeExportSource
from .store import ConversationStore, connect, default_db_path


def main() -> None:
    parser = argparse.ArgumentParser(description="claude.ai のエクスポートを SQLite に取り込む")
    parser.add_argument("path", type=Path, help="conversations.json、またはそれを含む zip")
    parser.add_argument("--db", type=Path, default=None, help="取り込み先（既定: $TSUDURI_DB または data/tsuduri.db）")
    args = parser.parse_args()

    db_path = args.db or default_db_path()
    conn = connect(db_path)
    try:
        result = ConversationStore(conn).import_conversations(ClaudeExportSource(args.path).load())
    finally:
        conn.close()

    print(
        f"{db_path} に取り込みました: 新規 {result.added} 件 / 更新 {result.updated} 件 / "
        f"変化なし {result.unchanged} 件 / 追加メッセージ {result.messages_added} 件 / "
        f"消えた本文・メッセージの印 {result.notes_added} 件"
    )
