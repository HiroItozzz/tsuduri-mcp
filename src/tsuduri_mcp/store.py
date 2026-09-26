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

-- LLM による要約とブログの下書き。同じ範囲・種類・モデル・プロンプトなら作り直さずに使い回す
CREATE TABLE IF NOT EXISTS summaries (
    id                 INTEGER PRIMARY KEY,
    conversation_uuid  TEXT NOT NULL REFERENCES conversations(uuid),
    first_message_uuid TEXT NOT NULL REFERENCES messages(uuid),  -- 1本の線の上の範囲の始まり
    last_message_uuid  TEXT NOT NULL REFERENCES messages(uuid),  -- 範囲の終わり
    kind               TEXT NOT NULL,  -- 'summary' / 'blog_draft'
    model              TEXT NOT NULL,
    prompt_hash        TEXT NOT NULL,  -- プロンプトを直したら作り直すため
    content            TEXT NOT NULL,  -- 要約は本文、下書きは JSON
    input_tokens       INTEGER NOT NULL,
    output_tokens      INTEGER NOT NULL,
    created_at         TEXT NOT NULL,
    UNIQUE (first_message_uuid, last_message_uuid, kind, model, prompt_hash)
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
    conn.row_factory = sqlite3.Row  # 列を名前で取り出す
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
    text_message_count: int  # 本文のあるメッセージの数。エクスポートに本文が含まれない会話がある
    first_human_text: str  # タイトルが空の会話を見分けるため


@dataclass
class PositionedMessage:
    position: int
    parent_position: int | None
    message: Message


@dataclass
class SummaryKey:
    conversation_uuid: str
    first_message_uuid: str
    last_message_uuid: str
    kind: str
    model: str
    prompt_hash: str


@dataclass
class Summary:
    key: SummaryKey
    content: str
    input_tokens: int
    output_tokens: int
    created_at: str


@dataclass
class Line:
    """会話の中の1本の線（枝分かれを1つに決めたもの）。"""

    messages: list[PositionedMessage]
    leaf_count: int  # 会話全体の枝の数。1 なら枝分かれなし


@dataclass
class Page[T]:
    total: int
    items: list[T]


class _Where:
    """WHERE 句と、そのプレースホルダに渡す値を組み立てる。"""

    def __init__(self) -> None:
        self.clauses: list[str] = []
        self.params: list[object] = []

    def add(self, clause: str, *params: object) -> None:
        self.clauses.append(clause)
        self.params.extend(params)

    def add_if_given(self, clause: str, value: object) -> None:
        if value is not None:
            self.add(clause, value)

    @property
    def sql(self) -> str:
        return " AND ".join(self.clauses) or "1"


def _like_pattern(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _text_contains(term: str) -> tuple[str, str]:
    """本文に term を含む、という条件の SQL と値。"""
    if len(term) >= FTS_MIN_CHARS:
        phrase = '"' + term.replace('"', '""') + '"'  # フレーズとして渡し、FTS の演算子を無効にする
        return "m.id IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)", phrase
    return "m.text LIKE ? ESCAPE '\\'", _like_pattern(term)


def _direction(order: Literal["newest", "oldest"]) -> str:
    return "DESC" if order == "newest" else "ASC"


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
                    m.uuid,
                    conv.uuid,
                    i,
                    m.parent_uuid,
                    m.sender,
                    m.text,
                    m.created_at,
                    m.updated_at,
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
        where = _Where()
        conditions = [_text_contains(k) for k in keywords if k]
        if conditions:
            joiner = " AND " if match == "all" else " OR "
            where.add("(" + joiner.join(sql for sql, _ in conditions) + ")", *(value for _, value in conditions))
        for term in exclude:
            if term:
                sql, value = _text_contains(term)
                where.add(f"NOT {sql}", value)
        where.add_if_given("m.sender = ?", sender)
        where.add_if_given("m.created_at >= ?", since)
        where.add_if_given("m.created_at < ?", until)
        where.add_if_given("m.conversation_uuid = ?", conversation_uuid)

        total = self.conn.execute(f"SELECT count(*) FROM messages m WHERE {where.sql}", where.params).fetchone()[0]
        rows = self.conn.execute(
            f"""SELECT m.conversation_uuid, c.name AS conversation_name, m.position, m.sender, m.created_at, m.text
                FROM messages m JOIN conversations c ON c.uuid = m.conversation_uuid
                WHERE {where.sql}
                ORDER BY m.created_at {_direction(order)}, m.position {_direction(order)}
                LIMIT ? OFFSET ?""",
            [*where.params, limit, offset],
        ).fetchall()
        return Page(total, [MessageHit(**row) for row in rows])

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
        """期間にやりとりのあった会話（since 以上 until 未満に作られたメッセージがある会話）を返す。

        会話の updated_at は、タイトルの変更などメッセージのない操作でも新しくなるので、期間の判定には使わない。
        """
        where = _Where()
        if since is not None or until is not None:
            where.add(
                """EXISTS (SELECT 1 FROM messages m WHERE m.conversation_uuid = c.uuid
                           AND m.created_at >= ? AND m.created_at < ?)""",
                since or "",
                until or "9999",
            )
        where.add_if_given("c.uuid = ?", uuid)
        if title:
            where.add("c.name LIKE ? ESCAPE '\\'", _like_pattern(title))

        total = self.conn.execute(f"SELECT count(*) FROM conversations c WHERE {where.sql}", where.params).fetchone()[0]
        rows = self.conn.execute(
            f"""SELECT c.uuid, c.name, c.created_at, c.updated_at,
                       (SELECT count(*) FROM messages m WHERE m.conversation_uuid = c.uuid) AS message_count,
                       (SELECT count(*) FROM messages m WHERE m.conversation_uuid = c.uuid AND m.text != '')
                           AS text_message_count,
                       coalesce((SELECT m.text FROM messages m
                                 WHERE m.conversation_uuid = c.uuid AND m.sender = 'human' AND m.text != ''
                                 ORDER BY m.position LIMIT 1), '') AS first_human_text
                FROM conversations c WHERE {where.sql}
                ORDER BY c.updated_at {_direction(order)}
                LIMIT ? OFFSET ?""",
            [*where.params, limit, offset],
        ).fetchall()
        return Page(total, [ConversationSummary(**row) for row in rows])

    def get_messages(self, conversation_uuid: str, start: int = 0, count: int | None = None) -> list[PositionedMessage]:
        rows = self.conn.execute(
            """SELECT m.position, p.position AS parent_position,
                      m.uuid, m.sender, m.text, m.created_at, m.updated_at, m.parent_uuid,
                      m.raw_content, m.attachments, m.files
               FROM messages m LEFT JOIN messages p ON p.uuid = m.parent_uuid
               WHERE m.conversation_uuid = ? AND m.position >= ?
               ORDER BY m.position
               LIMIT ?""",
            (conversation_uuid, start, -1 if count is None else count),  # LIMIT -1 は上限なし
        ).fetchall()
        return [
            PositionedMessage(
                position=row["position"],
                parent_position=row["parent_position"],
                message=Message(
                    uuid=row["uuid"],
                    sender=row["sender"],
                    text=row["text"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    parent_uuid=row["parent_uuid"],
                    raw_content=json.loads(row["raw_content"]),
                    attachments=json.loads(row["attachments"]),
                    files=json.loads(row["files"]),
                ),
            )
            for row in rows
        ]

    def get_line(self, conversation_uuid: str, through_index: int | None = None) -> Line:
        """through_index のメッセージを通る線を返す。

        そのメッセージより前は親をたどり、後はいちばん新しい続きをたどる。
        through_index を省くと、会話でいちばん新しいメッセージを通る線（本線）になる。
        """
        messages = self.get_messages(conversation_uuid)
        if not messages:
            return Line([], 0)
        by_uuid = {pm.message.uuid: pm for pm in messages}
        children: dict[str, list[PositionedMessage]] = {}
        for pm in messages:
            if pm.message.parent_uuid in by_uuid:
                children.setdefault(pm.message.parent_uuid, []).append(pm)

        def newest(pms: Iterable[PositionedMessage]) -> PositionedMessage:
            return max(pms, key=lambda pm: (pm.message.created_at, pm.position))

        if through_index is None:
            through = newest(messages)
        else:
            found = [pm for pm in messages if pm.position == through_index]
            if not found:
                raise ValueError(f"index={through_index} のメッセージはありません（0〜{len(messages) - 1}）")
            through = found[0]

        # 後ろ: through の子孫のうち、いちばん新しいものを終点にする
        descendants, stack = [through], [through]
        while stack:
            kids = children.get(stack.pop().message.uuid, [])
            descendants += kids
            stack += kids
        leaf: PositionedMessage | None = newest(descendants)

        # 前: 終点から親をたどる
        line = []
        while leaf is not None:
            line.append(leaf)
            parent = leaf.message.parent_uuid
            leaf = by_uuid.get(parent) if parent is not None else None
        leaf_count = sum(1 for pm in messages if pm.message.uuid not in children)
        return Line(line[::-1], leaf_count)

    # --- 要約 ---

    def find_summary(self, key: SummaryKey) -> Summary | None:
        row = self.conn.execute(
            """SELECT content, input_tokens, output_tokens, created_at FROM summaries
               WHERE first_message_uuid = ? AND last_message_uuid = ? AND kind = ? AND model = ? AND prompt_hash = ?""",
            (key.first_message_uuid, key.last_message_uuid, key.kind, key.model, key.prompt_hash),
        ).fetchone()
        return None if row is None else Summary(key, **row)

    def save_summary(self, summary: Summary) -> None:
        """同じキーがあれば上書きする（作り直したとき）。"""
        key = summary.key
        with self.conn:
            self.conn.execute(
                """INSERT INTO summaries (conversation_uuid, first_message_uuid, last_message_uuid, kind, model,
                                          prompt_hash, content, input_tokens, output_tokens, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (first_message_uuid, last_message_uuid, kind, model, prompt_hash) DO UPDATE SET
                       content = excluded.content, input_tokens = excluded.input_tokens,
                       output_tokens = excluded.output_tokens, created_at = excluded.created_at""",
                (
                    key.conversation_uuid,
                    key.first_message_uuid,
                    key.last_message_uuid,
                    key.kind,
                    key.model,
                    key.prompt_hash,
                    summary.content,
                    summary.input_tokens,
                    summary.output_tokens,
                    summary.created_at,
                ),
            )

    def get_conversation(self, uuid: str) -> Conversation | None:
        row = self.conn.execute(
            "SELECT uuid, name, summary, created_at, updated_at FROM conversations WHERE uuid = ?", (uuid,)
        ).fetchone()
        if row is None:
            return None
        return Conversation(**row, messages=[pm.message for pm in self.get_messages(uuid)])
