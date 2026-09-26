import json
import os
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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
    id                INTEGER PRIMARY KEY,  -- 全文検索の索引が参照する。VACUUM しても変わらない
    uuid              TEXT NOT NULL UNIQUE,
    conversation_uuid TEXT NOT NULL REFERENCES conversations(uuid),
    position          INTEGER NOT NULL,     -- エクスポート内での並び順（0 始まり）
    parent_uuid       TEXT,
    sender            TEXT NOT NULL,
    text              TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    raw_content       TEXT NOT NULL,        -- JSON
    attachments       TEXT NOT NULL,        -- JSON
    files             TEXT NOT NULL         -- JSON
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_uuid, position);
CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at);

-- trigram は3文字以上の部分一致に使える。2文字以下は LIKE で探す
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, content='messages', content_rowid='id', tokenize='trigram'
);

-- メッセージは追加だけで、更新・削除はしない（docs/design.md）
CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;
"""

FTS_MIN_CHARS = 3


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


@dataclass
class MessageHit:
    conversation_uuid: str
    conversation_name: str
    position: int
    sender: str
    created_at: str
    text: str


@dataclass
class ConversationSummary:
    uuid: str
    name: str
    created_at: str
    updated_at: str
    message_count: int
    first_human_text: str  # タイトルが空の会話を見分けるため


@dataclass
class PositionedMessage:
    position: int
    parent_position: int | None
    message: Message


@dataclass
class Page[T]:
    total: int
    items: list[T]


class ConversationStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.executescript(SCHEMA)

    # --- 取り込み ---

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

    def _upsert_conversation(self, conv: Conversation) -> None:
        self.conn.execute(
            """INSERT INTO conversations (uuid, name, summary, created_at, updated_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(uuid) DO UPDATE SET
                   name = excluded.name, summary = excluded.summary, updated_at = excluded.updated_at""",
            (conv.uuid, conv.name, conv.summary, conv.created_at, conv.updated_at),
        )

    def _insert_messages(self, conv: Conversation) -> int:
        cursor = self.conn.executemany(
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
        # rowcount はトリガー（FTS への書き込み）の分を含まない。total_changes は含むので使わない
        return cursor.rowcount

    # --- 読み出し ---

    def search_messages(
        self,
        keywords: Sequence[str],
        *,
        match: Literal["all", "any"] = "all",
        exclude: Sequence[str] = (),
        sender: str | None = None,
        since: str | None = None,
        until: str | None = None,
        conversation_uuid: str | None = None,
        order: Literal["newest", "oldest"] = "newest",
        limit: int = 20,
        offset: int = 0,
    ) -> Page[MessageHit]:
        """本文の部分一致で探す。since は以上、until は未満（どちらも UTC の ISO 文字列）。"""
        where, params = [], []
        terms = [k for k in keywords if k]
        if terms:
            conds = [self._contains(t, params) for t in terms]
            where.append("(" + (" AND " if match == "all" else " OR ").join(conds) + ")")
        for term in (e for e in exclude if e):
            where.append("NOT " + self._contains(term, params))
        for sql, value in (
            ("m.sender = ?", sender),
            ("m.created_at >= ?", since),
            ("m.created_at < ?", until),
            ("m.conversation_uuid = ?", conversation_uuid),
        ):
            if value is not None:
                where.append(sql)
                params.append(value)
        where_sql = " AND ".join(where) or "1"
        direction = "DESC" if order == "newest" else "ASC"

        total = self.conn.execute(f"SELECT count(*) FROM messages m WHERE {where_sql}", params).fetchone()[0]
        rows = self.conn.execute(
            f"""SELECT m.conversation_uuid, c.name, m.position, m.sender, m.created_at, m.text
                FROM messages m JOIN conversations c ON c.uuid = m.conversation_uuid
                WHERE {where_sql}
                ORDER BY m.created_at {direction}, m.position {direction}
                LIMIT ? OFFSET ?""",
            [*params, limit, offset],
        ).fetchall()
        return Page(total, [MessageHit(*r) for r in rows])

    @staticmethod
    def _contains(term: str, params: list) -> str:
        if len(term) >= FTS_MIN_CHARS:
            params.append('"' + term.replace('"', '""') + '"')  # フレーズとして渡し、FTS の演算子を無効にする
            return "m.id IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)"
        params.append("%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        return "m.text LIKE ? ESCAPE '\\'"

    def list_conversations(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        title: str | None = None,
        uuid: str | None = None,
        order: Literal["newest", "oldest"] = "newest",
        limit: int = 50,
        offset: int = 0,
    ) -> Page[ConversationSummary]:
        """期間に動きのあった会話（作成が until より前、かつ最終更新が since 以降）を返す。"""
        where, params = [], []
        for sql, value in (("c.updated_at >= ?", since), ("c.created_at < ?", until), ("c.uuid = ?", uuid)):
            if value is not None:
                where.append(sql)
                params.append(value)
        if title:
            where.append("c.name LIKE ? ESCAPE '\\'")
            params.append("%" + title.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        where_sql = " AND ".join(where) or "1"
        direction = "DESC" if order == "newest" else "ASC"

        total = self.conn.execute(f"SELECT count(*) FROM conversations c WHERE {where_sql}", params).fetchone()[0]
        rows = self.conn.execute(
            f"""SELECT c.uuid, c.name, c.created_at, c.updated_at,
                       (SELECT count(*) FROM messages m WHERE m.conversation_uuid = c.uuid),
                       coalesce((SELECT m.text FROM messages m
                                 WHERE m.conversation_uuid = c.uuid AND m.sender = 'human' AND m.text != ''
                                 ORDER BY m.position LIMIT 1), '')
                FROM conversations c WHERE {where_sql}
                ORDER BY c.updated_at {direction}
                LIMIT ? OFFSET ?""",
            [*params, limit, offset],
        ).fetchall()
        return Page(total, [ConversationSummary(*r) for r in rows])

    def get_messages(self, conversation_uuid: str, start: int = 0, count: int | None = None) -> list[PositionedMessage]:
        rows = self.conn.execute(
            """SELECT m.position, p.position, m.uuid, m.sender, m.text, m.created_at, m.updated_at, m.parent_uuid,
                      m.raw_content, m.attachments, m.files
               FROM messages m LEFT JOIN messages p ON p.uuid = m.parent_uuid
               WHERE m.conversation_uuid = ? AND m.position >= ?
               ORDER BY m.position
               LIMIT ?""",
            (conversation_uuid, start, -1 if count is None else count),
        ).fetchall()
        return [
            PositionedMessage(
                position=r[0],
                parent_position=r[1],
                message=Message(
                    *r[2:8], raw_content=json.loads(r[8]), attachments=json.loads(r[9]), files=json.loads(r[10])
                ),
            )
            for r in rows
        ]

    def get_conversation(self, uuid: str) -> Conversation | None:
        row = self.conn.execute(
            "SELECT uuid, name, summary, created_at, updated_at FROM conversations WHERE uuid = ?", (uuid,)
        ).fetchone()
        if row is None:
            return None
        return Conversation(*row, messages=[pm.message for pm in self.get_messages(uuid)])
