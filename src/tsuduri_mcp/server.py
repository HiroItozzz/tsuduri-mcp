import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from . import render
from .dates import since_to_db, until_to_db
from .store import ConversationStore, ConversationSummary, connect, default_db_path

INSTRUCTIONS = """\
claude.ai の過去の会話履歴を検索・閲覧するサーバー。
- 話題から探すときは search_messages。当たったメッセージの conversation と index が返る
- 期間で探すときは list_conversations（例: 先週の会話）
- 前後の流れは get_messages で、index を指定して必要な範囲だけ読む
- 長い会話を丸ごと読むときは、AI がファイルを読める環境なら export_conversation でファイルに書き出してから読む
- 日付はローカル時刻の YYYY-MM-DD（または ISO 8601 の日時）で指定する。表示もローカル時刻
- index は会話の中でのメッセージの番号（0 始まり）
"""

MAX_LIMIT = 100
EXPORT_DIR = Path(tempfile.gettempdir()) / "tsuduri-mcp"

mcp = MCPServer("tsuduri", instructions=INSTRUCTIONS)

# 期間の引数の説明は共通
Since = Annotated[str | None, Field(description="この日時以降（YYYY-MM-DD はその日の 0 時から）")]
Until = Annotated[str | None, Field(description="この日時まで（YYYY-MM-DD はその日を含む）")]


@contextmanager
def open_store() -> Iterator[ConversationStore]:
    path = default_db_path()
    if not path.exists():
        raise FileNotFoundError(f"DB がありません: {path}。先に `uv run tsuduri-import <エクスポートの zip>` で取り込んでください")
    with closing(connect(path)) as conn:
        yield ConversationStore(conn)


def find_conversation(store: ConversationStore, conversation_uuid: str) -> ConversationSummary:
    found = store.list_conversations(uuid=conversation_uuid, limit=1).items
    if not found:
        raise ValueError(f"会話が見つかりません: {conversation_uuid}")
    return found[0]


@mcp.tool(structured_output=False)
def search_messages(
    keywords: Annotated[list[str], Field(description="探す言葉。部分一致で、大文字・小文字は区別しない")],
    match: Annotated[Literal["all", "any"], Field(description="all: すべて含む / any: どれかを含む")] = "all",
    exclude: Annotated[list[str] | None, Field(description="この言葉を含むメッセージは除く")] = None,
    sender: Annotated[Literal["human", "assistant"] | None, Field(description="発言者で絞る")] = None,
    since: Since = None,
    until: Until = None,
    conversation_uuid: Annotated[str | None, Field(description="この会話の中だけを探す")] = None,
    order: Literal["newest", "oldest"] = "newest",
    limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = 20,
    offset: Annotated[int, Field(ge=0)] = 0,
    max_chars: Annotated[int, Field(ge=50, description="1件あたりの最大文字数。長い本文はキーワードのまわりを切り出す")] = 800,
) -> str:
    """過去の会話のメッセージを、本文のキーワードで検索する。

    当たったメッセージごとに、会話の uuid・メッセージの index・発言者・日時・会話のタイトルと本文を返す。
    本文は text の部分だけで、thinking やツールの入出力は含まない。
    """
    with open_store() as store:
        page = store.search_messages(
            keywords,
            match=match,
            exclude=exclude or (),
            sender=sender,
            since=since and since_to_db(since),
            until=until and until_to_db(until),
            conversation_uuid=conversation_uuid,
            order=order,
            limit=limit,
            offset=offset,
        )
    return render.render_search(page, offset, max_chars, keywords)


@mcp.tool(structured_output=False)
def list_conversations(
    since: Since = None,
    until: Until = None,
    title: Annotated[str | None, Field(description="タイトルに含まれる言葉")] = None,
    order: Annotated[Literal["newest", "oldest"], Field(description="最終更新の順")] = "newest",
    limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = 50,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> str:
    """会話の一覧を返す（uuid・タイトル・作成日時・最終更新日時・メッセージ数）。

    期間は「その期間にやりとりのあった会話」で絞る。つまり、期間より前に始まって期間中に続きを話した会話も含む。
    タイトルが空の会話は、最初の発言の冒頭を代わりに表示する。
    """
    with open_store() as store:
        page = store.list_conversations(
            since=since and since_to_db(since),
            until=until and until_to_db(until),
            title=title,
            order=order,
            limit=limit,
            offset=offset,
        )
    return render.render_conversation_list(page, offset)


@mcp.tool(structured_output=False)
def get_messages(
    conversation_uuid: str,
    start: Annotated[int, Field(ge=0, description="最初の index")] = 0,
    count: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = 10,
    max_chars: Annotated[int | None, Field(ge=50, description="1件あたりの最大文字数。null なら省略しない")] = 2000,
) -> str:
    """会話のメッセージを index の順に、start から count 件だけ返す。

    検索で当たったメッセージの前後を読むときは、start をその index の少し前にする。
    編集や再生成で枝分かれした会話は、すべての枝が並んでいる。直前のメッセージ以外への返信には「分岐」と表示する。
    """
    with open_store() as store:
        info = find_conversation(store, conversation_uuid)
        messages = store.get_messages(conversation_uuid, start, count)
    return render.render_messages(info, messages, start, max_chars)


@mcp.tool(structured_output=False)
def export_conversation(
    conversation_uuid: str,
    include_details: Annotated[bool, Field(description="thinking とツール呼び出しの名前も書き出す")] = False,
) -> str:
    """会話の全文を Markdown ファイルに書き出して、そのパスを返す。

    本文はコンテキストに入れずにファイルへ出すので、長い会話でも安い。
    ファイルを読めるクライアント（Claude Code など）で、必要なところだけ読んだり検索したりするときに使う。
    """
    with open_store() as store:
        info = find_conversation(store, conversation_uuid)
        messages = store.get_messages(conversation_uuid)
    text = render.render_markdown(info, messages, include_details)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = EXPORT_DIR / f"{conversation_uuid}.md"
    path.write_text(text, encoding="utf-8")
    return f"{path} に書き出しました（{info.message_count} 件、{len(text)} 文字、{text.count(chr(10))} 行）"


def main() -> None:
    mcp.run()  # 既定は stdio。stdout は通信に使われるので print してはいけない

