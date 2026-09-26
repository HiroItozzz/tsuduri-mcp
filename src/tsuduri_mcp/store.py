import json
import os
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .dates import now_db
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

-- 新しいエクスポートで本文やメッセージが消えていたことの印。DB には古いデータが残るので、印で気づけるようにする
CREATE TABLE IF NOT EXISTS message_notes (
    message_uuid TEXT NOT NULL REFERENCES messages(uuid),
    kind         TEXT NOT NULL,  -- 'content_missing' / 'message_missing'
    noticed_at   TEXT NOT NULL,  -- 最初に気づいた日時（UTC、DB と同じ形）
    PRIMARY KEY (message_uuid, kind)
);

-- 会話ごとの本線（through_index なしの get_line）に乗っているメッセージ。取り込みのたびに作り直す
CREATE TABLE IF NOT EXISTS main_line_messages (
    message_uuid TEXT PRIMARY KEY REFERENCES messages(uuid)
);

-- ブログ投稿の記録。post_blog_article が投稿してそのまま記録する。手で投稿したときは record_blog_post で記録する
CREATE TABLE IF NOT EXISTS posts (
    id                INTEGER PRIMARY KEY,
    conversation_uuid TEXT NOT NULL REFERENCES conversations(uuid),
    service           TEXT NOT NULL,  -- 'hatena' / 'qiita' / 'devto' / 'other'
    url               TEXT NOT NULL,
    title             TEXT NOT NULL,
    posted_at         TEXT NOT NULL   -- 記録した日時（投稿した日時ではない）
);

-- 投稿に使った範囲（1本の線の上の、start〜end の index のメッセージ全部）
CREATE TABLE IF NOT EXISTS post_messages (
    post_id      INTEGER NOT NULL REFERENCES posts(id),
    message_uuid TEXT NOT NULL REFERENCES messages(uuid),
    PRIMARY KEY (post_id, message_uuid)
);

CREATE INDEX IF NOT EXISTS idx_post_messages_message ON post_messages(message_uuid);

-- Gemini を実際に呼んだときだけ1行記録する（保存済みの要約・下書きを使ったときは記録しない）
CREATE TABLE IF NOT EXISTS llm_calls (
    id                INTEGER PRIMARY KEY,
    kind              TEXT NOT NULL,  -- 'summary' / 'blog_draft'
    conversation_uuid TEXT NOT NULL REFERENCES conversations(uuid),
    model             TEXT NOT NULL,
    input_tokens      INTEGER NOT NULL,
    output_tokens     INTEGER NOT NULL,
    cost_usd          REAL,           -- わからなければ NULL
    created_at        TEXT NOT NULL
);
"""

FTS_MIN_CHARS = 3


def default_db_path() -> Path:
    return Path(os.environ.get("TSUDURI_DB", DEFAULT_DB_PATH))


def connect(path: Path | str) -> sqlite3.Connection:
    is_file_db = str(path) != ":memory:"
    if is_file_db:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row  # 列を名前で取り出す
    conn.execute("PRAGMA foreign_keys = ON")
    if is_file_db:
        # :memory: には効かない（WAL はファイルが前提）ので、ファイルの DB のときだけ設定する
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@dataclass
class ImportResult:
    added: int = 0  # 新しく入った会話
    updated: int = 0  # updated_at が新しくなっていた会話
    unchanged: int = 0
    messages_added: int = 0
    notes_added: int = 0  # 新しく気づいた「本文・メッセージが消えている」の印


@dataclass
class MessageHit:
    conversation_uuid: str
    conversation_name: str
    position: int
    sender: str
    created_at: str
    text: str
    on_main_line: bool
    posted: bool


@dataclass
class ConversationSummary:
    uuid: str
    name: str
    created_at: str
    updated_at: str
    message_count: int
    text_message_count: int  # 本文のあるメッセージの数。エクスポートに本文が含まれない会話がある
    first_human_text: str  # タイトルが空の会話を見分けるため
    last_message_at: str  # 最後のメッセージの created_at。メッセージが0件なら updated_at
    post_count: int  # この会話に記録されたブログ投稿の数
    main_line_unposted_count: int  # 本線のメッセージのうち、まだ投稿に含まれていない数


@dataclass
class MessageNote:
    """新しいエクスポートで本文やメッセージが消えていたことの印。"""

    kind: str
    noticed_at: str


@dataclass
class PositionedMessage:
    position: int
    parent_position: int | None
    message: Message
    notes: list[MessageNote] = field(default_factory=list)


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
class LlmCall:
    """実際に Gemini を呼んだ記録（llm_calls の1行）。料金の集計・表示に使う。"""

    kind: str
    conversation_uuid: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    created_at: str


@dataclass
class PostRecord:
    """記録されたブログ投稿。draft_blog_post が「過去にここまで投稿した」と知らせるのに使う。"""

    service: str
    url: str
    title: str
    min_position: int  # 投稿に使ったメッセージのうち、いちばん古いものの position
    max_position: int  # いちばん新しいものの position
    message_uuids: frozenset[str]


@dataclass
class PostOverlap:
    """記録しようとしている範囲のうち、すでに投稿として記録されている分。"""

    service: str
    title: str
    count: int  # 重なっているメッセージの数


@dataclass
class Line:
    """会話の中の1本の線（枝分かれを1つに決めたもの）。"""

    messages: list[PositionedMessage]
    leaf_count: int  # 会話全体の枝の数。1 なら枝分かれなし


@dataclass
class _Node:
    """線を選ぶのに必要な列だけを持つ、軽いメッセージの表現。"""

    uuid: str
    parent_uuid: str | None
    created_at: str
    position: int


def _select_line(nodes: Sequence[_Node], through_index: int | None = None) -> list[str]:
    """through_index のメッセージを通る線を選び、position 順（古い→新しい）の uuid を返す。

    そのメッセージより前は親をたどり、後はいちばん新しい続きをたどる。
    through_index を省くと、会話でいちばん新しいメッセージを通る線（本線）になる。
    nodes が空なら空のリストを返す。
    """
    if not nodes:
        return []
    by_uuid = {n.uuid: n for n in nodes}
    children: dict[str, list[_Node]] = {}
    for n in nodes:
        if n.parent_uuid in by_uuid:
            children.setdefault(n.parent_uuid, []).append(n)

    def newest(ns: Iterable[_Node]) -> _Node:
        return max(ns, key=lambda n: (n.created_at, n.position))

    if through_index is None:
        through = newest(nodes)
    else:
        found = [n for n in nodes if n.position == through_index]
        if not found:
            raise ValueError(f"index={through_index} のメッセージはありません（0〜{len(nodes) - 1}）")
        through = found[0]

    # 後ろ: through の子孫のうち、いちばん新しいものを終点にする
    descendants, stack = [through], [through]
    while stack:
        kids = children.get(stack.pop().uuid, [])
        descendants += kids
        stack += kids
    leaf: _Node | None = newest(descendants)

    # 前: 終点から親をたどる
    line = []
    while leaf is not None:
        line.append(leaf)
        parent = leaf.parent_uuid
        leaf = by_uuid.get(parent) if parent is not None else None
    return [n.uuid for n in line[::-1]]


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


def _hit_from_row(row: sqlite3.Row) -> MessageHit:
    data = dict(row)
    data["on_main_line"] = bool(data["on_main_line"])
    data["posted"] = bool(data["posted"])
    return MessageHit(**data)


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
                    self._upsert_conversation(conv)
                elif conv.updated_at > row[0]:  # 日時はすべて同じ ISO 形式なので文字列のまま比べられる
                    result.updated += 1
                    self._upsert_conversation(conv)
                else:
                    result.unchanged += 1
                # 会話が unchanged でも、そのエクスポートにしかないメッセージがあるかもしれないので必ず入れる
                result.messages_added += self._insert_messages(conv)
                # 印の更新も、会話の追加・更新の有無にかかわらず、エクスポートに入っていた会話は必ず調べる
                result.notes_added += self._update_notes(conv)
            self._recompute_main_line()
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

    def _update_notes(self, conv: Conversation) -> int:
        """このエクスポートの会話について、本文・メッセージが消えていないかを調べ、印を付け外しする。

        新しく足した印の数を返す。
        """
        export_uuids = {m.uuid for m in conv.messages}
        db_texts: dict[str, str] = dict(
            self.conn.execute("SELECT uuid, text FROM messages WHERE conversation_uuid = ?", (conv.uuid,)).fetchall()
        )
        noticed_at = now_db()
        added = 0
        for m in conv.messages:
            if not m.raw_content and db_texts.get(m.uuid, ""):
                added += self._add_note(m.uuid, "content_missing", noticed_at)
            else:
                self._remove_note(m.uuid, "content_missing")
        for uuid in db_texts:
            if uuid not in export_uuids:
                added += self._add_note(uuid, "message_missing", noticed_at)
        for uuid in export_uuids:
            self._remove_note(uuid, "message_missing")
        return added

    def _add_note(self, message_uuid: str, kind: str, noticed_at: str) -> int:
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO message_notes (message_uuid, kind, noticed_at) VALUES (?, ?, ?)",
            (message_uuid, kind, noticed_at),
        )
        return cursor.rowcount  # 0 ならすでに印があった（noticed_at はそのまま）

    def _remove_note(self, message_uuid: str, kind: str) -> None:
        self.conn.execute("DELETE FROM message_notes WHERE message_uuid = ? AND kind = ?", (message_uuid, kind))

    def _recompute_main_line(self) -> None:
        """全会話の本線（through_index なしの get_line）を計算し直し、main_line_messages を入れ替える。

        本文や raw_content は読まず、線を選ぶのに必要な列だけを1回のクエリで読む。
        """
        rows = self.conn.execute(
            "SELECT conversation_uuid, uuid, parent_uuid, created_at, position FROM messages ORDER BY position"
        ).fetchall()
        by_conversation: dict[str, list[_Node]] = {}
        for row in rows:
            by_conversation.setdefault(row["conversation_uuid"], []).append(
                _Node(row["uuid"], row["parent_uuid"], row["created_at"], row["position"])
            )
        main_line_uuids = [uuid for nodes in by_conversation.values() for uuid in _select_line(nodes)]
        self.conn.execute("DELETE FROM main_line_messages")
        self.conn.executemany(
            "INSERT INTO main_line_messages (message_uuid) VALUES (?)", [(uuid,) for uuid in main_line_uuids]
        )

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
        main_line_only: bool = False,
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
        if main_line_only:
            # IN (SELECT …) だと本線の全件を先に走査する計画になり遅い（実データで約 2.5 秒）。
            # EXISTS なら、ほかの条件で絞った行だけを確かめる（約 0.002 秒）
            where.add("EXISTS (SELECT 1 FROM main_line_messages ml WHERE ml.message_uuid = m.uuid)")

        total = self.conn.execute(f"SELECT count(*) FROM messages m WHERE {where.sql}", where.params).fetchone()[0]
        rows = self.conn.execute(
            f"""SELECT m.conversation_uuid, c.name AS conversation_name, m.position, m.sender, m.created_at, m.text,
                       ml.message_uuid IS NOT NULL AS on_main_line,
                       EXISTS (SELECT 1 FROM post_messages pm WHERE pm.message_uuid = m.uuid) AS posted
                FROM messages m
                JOIN conversations c ON c.uuid = m.conversation_uuid
                LEFT JOIN main_line_messages ml ON ml.message_uuid = m.uuid
                WHERE {where.sql}
                ORDER BY m.created_at {_direction(order)}, m.position {_direction(order)}
                LIMIT ? OFFSET ?""",
            [*where.params, limit, offset],
        ).fetchall()
        return Page(total, [_hit_from_row(row) for row in rows])

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
                                 ORDER BY m.position LIMIT 1), '') AS first_human_text,
                       coalesce((SELECT m.created_at FROM messages m
                                 WHERE m.conversation_uuid = c.uuid
                                 ORDER BY m.position DESC LIMIT 1), c.updated_at) AS last_message_at,
                       (SELECT count(*) FROM posts p WHERE p.conversation_uuid = c.uuid) AS post_count,
                       (SELECT count(*) FROM main_line_messages ml
                                 JOIN messages m ON m.uuid = ml.message_uuid
                                 WHERE m.conversation_uuid = c.uuid
                                   AND NOT EXISTS
                                       (SELECT 1 FROM post_messages pm WHERE pm.message_uuid = ml.message_uuid)
                       ) AS main_line_unposted_count
                FROM conversations c WHERE {where.sql}
                ORDER BY last_message_at {_direction(order)}
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
        notes = self._notes_for([row["uuid"] for row in rows])
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
                notes=notes.get(row["uuid"], []),
            )
            for row in rows
        ]

    def _notes_for(self, message_uuids: Sequence[str]) -> dict[str, list[MessageNote]]:
        """複数メッセージぶんの印を、まとめて1回のクエリで読む。"""
        if not message_uuids:
            return {}
        placeholders = ",".join("?" * len(message_uuids))
        rows = self.conn.execute(
            f"SELECT message_uuid, kind, noticed_at FROM message_notes WHERE message_uuid IN ({placeholders})",
            message_uuids,
        ).fetchall()
        notes: dict[str, list[MessageNote]] = {}
        for row in rows:
            notes.setdefault(row["message_uuid"], []).append(MessageNote(row["kind"], row["noticed_at"]))
        return notes

    def get_line(self, conversation_uuid: str, through_index: int | None = None) -> Line:
        """through_index のメッセージを通る線を返す。

        そのメッセージより前は親をたどり、後はいちばん新しい続きをたどる。
        through_index を省くと、会話でいちばん新しいメッセージを通る線（本線）になる。
        """
        messages = self.get_messages(conversation_uuid)
        if not messages:
            return Line([], 0)
        nodes = [_Node(pm.message.uuid, pm.message.parent_uuid, pm.message.created_at, pm.position) for pm in messages]
        line_uuids = _select_line(nodes, through_index)
        by_uuid = {pm.message.uuid: pm for pm in messages}
        parents_with_children = {n.parent_uuid for n in nodes if n.parent_uuid in by_uuid}
        leaf_count = sum(1 for n in nodes if n.uuid not in parents_with_children)
        return Line([by_uuid[uuid] for uuid in line_uuids], leaf_count)

    # --- ブログ投稿の記録 ---

    def record_post(
        self, conversation_uuid: str, service: str, url: str, title: str, message_uuids: Sequence[str]
    ) -> int:
        """投稿を記録する（posts と post_messages を1トランザクションで書く）。作った post の id を返す。"""
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO posts (conversation_uuid, service, url, title, posted_at) VALUES (?, ?, ?, ?, ?)",
                (conversation_uuid, service, url, title, now_db()),
            )
            post_id = cursor.lastrowid
            self.conn.executemany(
                "INSERT INTO post_messages (post_id, message_uuid) VALUES (?, ?)",
                [(post_id, uuid) for uuid in message_uuids],
            )
        assert post_id is not None
        return post_id

    def find_posted_overlap(self, message_uuids: Sequence[str]) -> list[PostOverlap]:
        """message_uuids のうち、すでに投稿として記録されている分を、投稿ごとにまとめて返す。

        同じ範囲を別サービスに投稿することがあるので、二重記録そのものは止めず、知らせるためだけに使う。
        """
        if not message_uuids:
            return []
        placeholders = ",".join("?" * len(message_uuids))
        rows = self.conn.execute(
            f"""SELECT p.service, p.title, count(*) AS overlap_count
                FROM post_messages pm
                JOIN posts p ON p.id = pm.post_id
                WHERE pm.message_uuid IN ({placeholders})
                GROUP BY p.id
                ORDER BY p.id""",
            message_uuids,
        ).fetchall()
        return [PostOverlap(row["service"], row["title"], row["overlap_count"]) for row in rows]

    def list_posts(self, conversation_uuid: str) -> list[PostRecord]:
        """この会話に記録された投稿を、記録した順に返す。"""
        rows = self.conn.execute(
            """SELECT p.service, p.url, p.title,
                      min(m.position) AS min_position, max(m.position) AS max_position,
                      group_concat(pm.message_uuid) AS message_uuids
               FROM posts p
               JOIN post_messages pm ON pm.post_id = p.id
               JOIN messages m ON m.uuid = pm.message_uuid
               WHERE p.conversation_uuid = ?
               GROUP BY p.id
               ORDER BY p.id""",
            (conversation_uuid,),
        ).fetchall()
        return [
            PostRecord(
                service=row["service"],
                url=row["url"],
                title=row["title"],
                min_position=row["min_position"],
                max_position=row["max_position"],
                message_uuids=frozenset(row["message_uuids"].split(",")),
            )
            for row in rows
        ]

    def _posted_uuids(self, message_uuids: Sequence[str]) -> set[str]:
        """このメッセージ uuid のうち、すでに投稿の記録があるものを返す。"""
        if not message_uuids:
            return set()
        placeholders = ",".join("?" * len(message_uuids))
        rows = self.conn.execute(
            f"SELECT DISTINCT message_uuid FROM post_messages WHERE message_uuid IN ({placeholders})",
            message_uuids,
        ).fetchall()
        return {row["message_uuid"] for row in rows}

    def unposted_start(self, line_messages: Sequence[PositionedMessage]) -> int | None:
        """1本の線の上で、まだ投稿していない部分の始まりの index。

        投稿がなければ線の最初の index。全部投稿済みなら None（呼び出し側でエラーにする）。
        「次の index」は線の並びの上でのすぐ次のメッセージ（枝分かれで index が飛んでいても、この線に実在する index）。
        """
        if not line_messages:
            return 0
        posted = self._posted_uuids([pm.message.uuid for pm in line_messages])
        if not posted:
            return line_messages[0].position
        last_posted_i = max(i for i, pm in enumerate(line_messages) if pm.message.uuid in posted)
        if last_posted_i == len(line_messages) - 1:
            return None
        return line_messages[last_posted_i + 1].position

    # --- 要約 ---

    def find_summary(self, key: SummaryKey) -> Summary | None:
        row = self.conn.execute(
            """SELECT content, input_tokens, output_tokens, created_at FROM summaries
               WHERE first_message_uuid = ? AND last_message_uuid = ? AND kind = ? AND model = ? AND prompt_hash = ?""",
            (key.first_message_uuid, key.last_message_uuid, key.kind, key.model, key.prompt_hash),
        ).fetchone()
        return None if row is None else Summary(key, **row)

    def find_latest_draft(self, first_message_uuid: str, last_message_uuid: str) -> str | None:
        """この範囲のブログの下書き（JSON）のうち、いちばん新しいもの。モデルやプロンプトは問わない。"""
        row = self.conn.execute(
            """SELECT content FROM summaries
               WHERE first_message_uuid = ? AND last_message_uuid = ? AND kind = 'blog_draft'
               ORDER BY created_at DESC LIMIT 1""",
            (first_message_uuid, last_message_uuid),
        ).fetchone()
        return None if row is None else row["content"]

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

    def record_llm_call(
        self,
        kind: str,
        conversation_uuid: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float | None,
        created_at: str,
    ) -> None:
        """Gemini を実際に呼んだときだけ呼ぶ（保存済みを使い回したときは呼ばない）。"""
        with self.conn:
            self.conn.execute(
                """INSERT INTO llm_calls (kind, conversation_uuid, model, input_tokens, output_tokens,
                                          cost_usd, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (kind, conversation_uuid, model, input_tokens, output_tokens, cost_usd, created_at),
            )

    def list_llm_calls(self, conversation_uuid: str | None = None) -> list[LlmCall]:
        """記録された llm_calls を、記録した順に返す（テストや将来の集計用）。"""
        where = _Where()
        where.add_if_given("conversation_uuid = ?", conversation_uuid)
        rows = self.conn.execute(
            f"""SELECT kind, conversation_uuid, model, input_tokens, output_tokens, cost_usd, created_at
                FROM llm_calls WHERE {where.sql} ORDER BY id""",
            where.params,
        ).fetchall()
        return [LlmCall(**row) for row in rows]

    def get_conversation(self, uuid: str) -> Conversation | None:
        row = self.conn.execute(
            "SELECT uuid, name, summary, created_at, updated_at FROM conversations WHERE uuid = ?", (uuid,)
        ).fetchone()
        if row is None:
            return None
        return Conversation(**row, messages=[pm.message for pm in self.get_messages(uuid)])
