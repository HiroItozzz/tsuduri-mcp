import json
import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .models import Conversation, Message

DEFAULT_DB_PATH = "data/tsuduri.db"  # 相対パスは cwd 基準。MCP は `uv run --directory` で起動する前提

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    uuid       TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    summary    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    uuid              TEXT PRIMARY KEY,
    conversation_uuid TEXT NOT NULL REFERENCES conversations(uuid),
    position          INTEGER NOT NULL,  -- エクスポート内での並び順
    parent_uuid       TEXT,
    sender            TEXT NOT NULL,
    text              TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    raw_content       TEXT NOT NULL,     -- JSON
    attachments       TEXT NOT NULL,     -- JSON
    files             TEXT NOT NULL      -- JSON
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_uuid, position);
"""


def default_db_path() -> Path:
    return Path(os.environ.get("TSUDURI_DB", DEFAULT_DB_PATH))


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@dataclass
class ImportResult:
    added: int = 0  # 新しく入った会話
    updated: int = 0  # updated_at が新しくなっていた会話
    unchanged: int = 0
    messages_added: int = 0


class ConversationStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.executescript(SCHEMA)

    def import_conversations(self, conversations: Iterable[Conversation]) -> ImportResult:
        result = ImportResult()
        with self.conn:
            for conv in conversations:
                row = self.conn.execute("SELECT updated_at FROM conversations WHERE uuid = ?", (conv.uuid,)).fetchone()
                if row is None:
                    result.added += 1
                elif conv.updated_at > row[0]:  # 日時はすべて同じ ISO 形式なので文字列のまま比べられる
                    result.updated += 1
                else:
                    result.unchanged += 1
                    continue
                self._upsert_conversation(conv)
                result.messages_added += self._insert_messages(conv)
        return result

    def get_conversation(self, uuid: str) -> Conversation | None:
        row = self.conn.execute(
            "SELECT uuid, name, summary, created_at, updated_at FROM conversations WHERE uuid = ?", (uuid,)
        ).fetchone()
        if row is None:
            return None
        rows = self.conn.execute(
            """SELECT uuid, sender, text, created_at, updated_at, parent_uuid, raw_content, attachments, files
               FROM messages WHERE conversation_uuid = ? ORDER BY position""",
            (uuid,),
        ).fetchall()
        messages = [
            Message(*r[:6], raw_content=json.loads(r[6]), attachments=json.loads(r[7]), files=json.loads(r[8]))
            for r in rows
        ]
        return Conversation(*row, messages=messages)

    def _upsert_conversation(self, conv: Conversation) -> None:
        self.conn.execute(
            """INSERT INTO conversations (uuid, name, summary, created_at, updated_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(uuid) DO UPDATE SET
                   name = excluded.name, summary = excluded.summary, updated_at = excluded.updated_at""",
            (conv.uuid, conv.name, conv.summary, conv.created_at, conv.updated_at),
        )

    def _insert_messages(self, conv: Conversation) -> int:
        before = self.conn.total_changes
        self.conn.executemany(
            """INSERT OR IGNORE INTO messages
               (uuid, conversation_uuid, position, parent_uuid, sender, text, created_at, updated_at,
                raw_content, attachments, files)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    m.uuid, conv.uuid, i, m.parent_uuid, m.sender, m.text, m.created_at, m.updated_at,
                    json.dumps(m.raw_content, ensure_ascii=False),
                    json.dumps(m.attachments, ensure_ascii=False),
                    json.dumps(m.files, ensure_ascii=False),
                )
                for i, m in enumerate(conv.messages)
            ],
        )
        return self.conn.total_changes - before
