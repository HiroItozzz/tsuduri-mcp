import hashlib
import tempfile
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field
from pydantic_ai.models import Model

from . import llm, render
from .dates import now_db, since_to_db, until_to_db
from .store import ConversationStore, ConversationSummary, Summary, SummaryKey, connect, default_db_path

INSTRUCTIONS = """\
claude.ai の過去の会話履歴を検索・閲覧するサーバー。
- 話題から探すときは search_messages。当たったメッセージの conversation と index が返る
- 期間で探すときは list_conversations（例: 先週の会話）
- 前後の流れは get_messages で、index を指定して必要な範囲だけ読む
- 枝分かれした会話は、既定でいちばん新しい枝（本線）だけを扱う。別の枝は through_index で選ぶ
- 長い会話を丸ごと読むときは、AI がファイルを読める環境なら export_conversation でファイルに書き出してから読む
- 日付はローカル時刻の YYYY-MM-DD（または ISO 8601 の日時）で指定する。表示もローカル時刻
- index は会話の中でのメッセージの番号（0 始まり）
- 会話の中身を知りたいだけなら、summarize_conversation で Gemini に要約させるとコンテキストを節約できる
  （要約は保存され、2回目からは Gemini を呼ばない）
"""

MAX_LIMIT = 100
EXPORT_DIR = Path(tempfile.gettempdir()) / "tsuduri-mcp"
MAX_INPUT_CHARS = 600_000  # いちばん長い会話の本線でも約 36 万文字（2026-09 時点）

SUMMARY_MODEL = llm.DEFAULT_GEMINI_MODEL
make_model: Callable[[str], Model] = llm.gemini  # テストでは通信しないモデルに差し替える

mcp = MCPServer("tsuduri", instructions=INSTRUCTIONS)

# 期間の引数の説明は共通
Since = Annotated[str | None, Field(description="この日時以降（YYYY-MM-DD はその日の 0 時から）")]
Until = Annotated[str | None, Field(description="この日時まで（YYYY-MM-DD はその日を含む）")]


@contextmanager
def open_store() -> Iterator[ConversationStore]:
    path = default_db_path()
    if not path.exists():
        raise FileNotFoundError(
            f"DB がありません: {path}。先に `uv run tsuduri-import <エクスポートの zip>` で取り込んでください"
        )
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
    max_chars: Annotated[
        int, Field(ge=50, description="1件あたりの最大文字数。長い本文はキーワードのまわりを切り出す")
    ] = 800,
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


# 線の選び方の引数は共通
ThroughIndex = Annotated[
    int | None,
    Field(ge=0, description="この index のメッセージを通る枝を読む。省くといちばん新しいメッセージを通る枝（本線）"),
]
AllBranches = Annotated[bool, Field(description="true なら枝を選ばず、すべての枝を index 順に並べる")]


def load_scope(
    store: ConversationStore, conversation_uuid: str, through_index: int | None, all_branches: bool
) -> render.Scope:
    if all_branches:
        messages = store.get_messages(conversation_uuid)
        leaf_count = store.get_line(conversation_uuid).leaf_count
        return render.Scope(messages, all_branches=True, leaf_count=leaf_count)
    line = store.get_line(conversation_uuid, through_index)
    return render.Scope(line.messages, all_branches=False, leaf_count=line.leaf_count)


@mcp.tool(structured_output=False)
def get_messages(
    conversation_uuid: str,
    start: Annotated[int, Field(ge=0, description="この index 以降を読む")] = 0,
    count: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = 10,
    through_index: ThroughIndex = None,
    all_branches: AllBranches = False,
    max_chars: Annotated[int | None, Field(ge=50, description="1件あたりの最大文字数。null なら省略しない")] = 2000,
) -> str:
    """会話のメッセージを index の順に、start から count 件だけ返す。

    編集や再生成で枝分かれした会話は、既定では1本の枝（本線）だけを返す。
    検索で当たったメッセージの前後を読むときは、through_index にその index を、start にその少し前を渡す。
    """
    with open_store() as store:
        info = find_conversation(store, conversation_uuid)
        scope = load_scope(store, conversation_uuid, through_index, all_branches)
    return render.render_messages(info, scope, start, count, max_chars)


@mcp.tool(structured_output=False)
def export_conversation(
    conversation_uuid: str,
    through_index: ThroughIndex = None,
    all_branches: AllBranches = False,
    include_details: Annotated[bool, Field(description="thinking とツール呼び出しの名前も書き出す")] = False,
) -> str:
    """会話の全文を Markdown ファイルに書き出して、そのパスを返す。

    本文はコンテキストに入れずにファイルへ出すので、長い会話でも安い。
    ファイルを読めるクライアント（Claude Code など）で、必要なところだけ読んだり検索したりするときに使う。
    既定では1本の枝（本線）だけを書き出す。
    """
    with open_store() as store:
        info = find_conversation(store, conversation_uuid)
        scope = load_scope(store, conversation_uuid, through_index, all_branches)
    text = render.render_markdown(info, scope, include_details)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = EXPORT_DIR / f"{conversation_uuid}.md"
    path.write_text(text, encoding="utf-8")
    return f"{path} に書き出しました（{len(scope.messages)} 件、{len(text)} 文字、{text.count(chr(10))} 行）"


def prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


@mcp.tool(structured_output=False)
async def summarize_conversation(
    conversation_uuid: str,
    through_index: ThroughIndex = None,
    start: Annotated[int, Field(ge=0, description="この index 以降だけを要約する")] = 0,
    refresh: Annotated[bool, Field(description="保存済みの要約があっても作り直す")] = False,
) -> str:
    """会話の1本の枝を Gemini で要約する。

    原文をコンテキストに入れずに中身を把握できる。要約には話題ごとの index が付くので、
    詳しく読みたいところだけ get_messages で読みに行ける。
    同じ範囲の要約は保存してあり、2回目からは Gemini を呼ばずに返す。
    """
    with open_store() as store:
        info = find_conversation(store, conversation_uuid)
        messages = [pm for pm in store.get_line(conversation_uuid, through_index).messages if pm.position >= start]
    if not messages:
        raise ValueError(f"index={start} 以降のメッセージがありません")
    transcript = render.render_transcript(info, messages)
    if len(transcript) > MAX_INPUT_CHARS:
        raise ValueError(f"会話が長すぎます（{len(transcript)} 文字）。start で範囲をしぼってください")

    instructions = llm.load_prompt("summary")
    key = SummaryKey(
        conversation_uuid=conversation_uuid,
        first_message_uuid=messages[0].message.uuid,
        last_message_uuid=messages[-1].message.uuid,
        kind="summary",
        model=SUMMARY_MODEL,
        prompt_hash=prompt_hash(instructions),
    )
    if not refresh:
        with open_store() as store:
            cached = store.find_summary(key)
        if cached is not None:
            return render.render_summary(info, messages, cached, cached=True)

    result = await llm.generate(make_model(SUMMARY_MODEL), instructions, transcript, str)
    summary = Summary(key, result.output, result.input_tokens, result.output_tokens, now_db())
    with open_store() as store:
        store.save_summary(summary)
    return render.render_summary(info, messages, summary, cached=False)


def main() -> None:
    mcp.run()  # 既定は stdio。stdout は通信に使われるので print してはいけない
