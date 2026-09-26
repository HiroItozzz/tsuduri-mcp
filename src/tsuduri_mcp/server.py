import functools
import hashlib
import inspect
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field
from pydantic_ai.models import Model

from . import llm, render
from .dates import now_db, since_to_db, until_to_db
from .store import (
    ConversationStore,
    ConversationSummary,
    Line,
    PositionedMessage,
    Summary,
    SummaryKey,
    connect,
    default_db_path,
)

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
- ブログの下書きは draft_blog_post（Gemini）か prompt の draft_blog_with_claude で作る。どちらも投稿はしない
- 下書きを投稿したら record_blog_post で範囲を記録する。次の draft_blog_post は続きから作れる
"""

MAX_LIMIT = 100
EXPORT_DIR = Path(tempfile.gettempdir()) / "tsuduri-mcp"
MAX_INPUT_CHARS = 600_000  # いちばん長い会話の本線でも約 36 万文字（2026-09 時点）
MIN_MATERIAL_CHARS = 1000  # 下書きの材料（本文の合計）がこれ未満なら「材料が薄い」と注意する

SUMMARY_MODEL = llm.DEFAULT_GEMINI_MODEL
DRAFT_MODEL = llm.DEFAULT_GEMINI_MODEL
make_model: Callable[[str], Model] = llm.gemini  # テストでは通信しないモデルに差し替える

mcp = MCPServer("tsuduri", instructions=INSTRUCTIONS)

# MCPServer は ToolError 以外の例外の文言を消してクライアントに返すので、入口で ToolError に変える
CAUGHT_EXCEPTIONS = (ValueError, FileNotFoundError, RuntimeError)


def mcp_tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """`@mcp.tool(structured_output=False)` の代わりに使う、全ツール共通のデコレータ。

    引数の名前・説明・既定値（入力スキーマ）は元の関数のまま変わらない
    （functools.wraps で __wrapped__ を残し、シグネチャの取得はそちらに流れる）。
    """
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except CAUGHT_EXCEPTIONS as e:
                raise ToolError(str(e)) from e
    else:

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except CAUGHT_EXCEPTIONS as e:
                raise ToolError(str(e)) from e

    return mcp.tool(structured_output=False)(wrapper)


# 期間の引数の説明は共通
Since = Annotated[str | None, Field(description="この日時以降（YYYY-MM-DD はその日の 0 時から）")]
Until = Annotated[str | None, Field(description="この日時の手前まで（YYYY-MM-DD はその日を含む）")]


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


def _check_keyword_lengths(words: Sequence[str]) -> None:
    for w in words:
        if len("".join(w.split())) <= 1:  # 空白を除いて1文字以下
            raise ValueError(f"キーワードは2文字以上にしてください: {w!r}")


@mcp_tool
def search_messages(
    keywords: Annotated[
        list[str], Field(description="探す言葉（2文字以上）。部分一致で、英字（半角）の大文字・小文字は区別しない")
    ],
    match: Annotated[Literal["all", "any"], Field(description="all: すべて含む / any: どれかを含む")] = "all",
    exclude: Annotated[list[str] | None, Field(description="この言葉を含むメッセージは除く")] = None,
    sender: Annotated[Literal["human", "assistant"] | None, Field(description="発言者で絞る")] = None,
    since: Since = None,
    until: Until = None,
    conversation_uuid: Annotated[str | None, Field(description="この会話の中だけを探す")] = None,
    main_line_only: Annotated[
        bool, Field(description="true なら、枝分かれした会話の本線（いちばん新しい枝）のメッセージだけを探す")
    ] = False,
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
    keywords が空なら、条件に合うすべてのメッセージを返す（期間で眺めるときに使う）。
    """
    _check_keyword_lengths(keywords)
    _check_keyword_lengths(exclude or ())
    with open_store() as store:
        page = store.search_messages(
            keywords,
            match=match,
            exclude=exclude or (),
            sender=sender,
            since=since and since_to_db(since),
            until=until and until_to_db(until),
            conversation_uuid=conversation_uuid,
            main_line_only=main_line_only,
            order=order,
            limit=limit,
            offset=offset,
        )
    return render.render_search(page, offset, max_chars, keywords)


@mcp_tool
def list_conversations(
    since: Since = None,
    until: Until = None,
    title: Annotated[str | None, Field(description="タイトルに含まれる言葉")] = None,
    order: Annotated[Literal["newest", "oldest"], Field(description="最終発言の順")] = "newest",
    limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = 50,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> str:
    """会話の一覧を返す（uuid・タイトル・作成日時・最終発言日時・メッセージ数）。

    期間は「その期間に発言のあった会話」で絞る。期間より前に始まって期間中に続きを話した会話も含む。
    タイトルが空の会話は、最初の発言の冒頭を代わりに表示する。
    「本文なし」の会話は、エクスポートに本文が含まれていないので読んでも中身はない。
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

Service = Literal["hatena", "qiita", "devto", "other"]


def load_scope(
    store: ConversationStore, conversation_uuid: str, through_index: int | None, all_branches: bool
) -> render.Scope:
    if all_branches and through_index is not None:
        raise ValueError("all_branches と through_index は同時に指定できません")
    if all_branches:
        messages = store.get_messages(conversation_uuid)
        leaf_count = store.get_line(conversation_uuid).leaf_count
        return render.Scope(messages, all_branches=True, leaf_count=leaf_count)
    line = store.get_line(conversation_uuid, through_index)
    return render.Scope(line.messages, all_branches=False, leaf_count=line.leaf_count)


@mcp_tool
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


@mcp_tool
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
    # 枝ごとにファイルを分け、書き出すたびに他の枝を上書きしないようにする
    suffix = "all" if all_branches else f"to{scope.messages[-1].position if scope.messages else 0}"
    path = EXPORT_DIR / f"{conversation_uuid}-{suffix}.md"
    path.write_text(text, encoding="utf-8")
    return f"{path} に書き出しました（{len(scope.messages)} 件、{len(text)} 文字、{text.count(chr(10))} 行）"


TRANSCRIPT_VERSION = 2  # render_transcript（会話ログの形）を変えたら、この数字を上げてキャッシュを作り直させる


def prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def load_transcript(
    conversation_uuid: str, through_index: int | None, start: int
) -> tuple[ConversationSummary, list[PositionedMessage], str]:
    with open_store() as store:
        info = find_conversation(store, conversation_uuid)
        messages = [pm for pm in store.get_line(conversation_uuid, through_index).messages if pm.position >= start]
    if not messages:
        raise ValueError(f"index={start} 以降のメッセージがありません")
    transcript = render.render_transcript(info, messages)
    if len(transcript) > MAX_INPUT_CHARS:
        raise ValueError(f"会話が長すぎます（{len(transcript)} 文字）。start で範囲をしぼってください")
    return info, messages, transcript


@mcp_tool
async def summarize_conversation(
    conversation_uuid: str,
    through_index: ThroughIndex = None,
    start: Annotated[int, Field(ge=0, description="この index 以降だけを要約する（選んだ枝の上の index）")] = 0,
    refresh: Annotated[bool, Field(description="保存済みの要約があっても作り直す")] = False,
) -> str:
    """会話の1本の枝を Gemini で要約する。

    原文をコンテキストに入れずに中身を把握できる。要約には話題ごとの index が付くので、
    詳しく読みたいところだけ get_messages で読みに行ける。
    同じ範囲の要約は保存してあり、2回目からは Gemini を呼ばずに返す。
    """
    info, messages, transcript = load_transcript(conversation_uuid, through_index, start)

    instructions = llm.load_prompt("summary")
    key = SummaryKey(
        conversation_uuid=conversation_uuid,
        first_message_uuid=messages[0].message.uuid,
        last_message_uuid=messages[-1].message.uuid,
        kind="summary",
        model=SUMMARY_MODEL,
        prompt_hash=prompt_hash(f"{TRANSCRIPT_VERSION}\n{instructions}"),
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


def resolve_draft_start(store: ConversationStore, line: Line, start: int | None) -> tuple[int, str | None]:
    """draft_blog_post の start。省いたら、線の上でまだ投稿していない部分の始まりにする。

    飛ばした範囲があれば注記も返す（なければ None）。
    """
    if start is not None:
        return start, None
    unposted = store.unposted_start(line.messages)
    if unposted is None:
        raise ValueError("この枝はすでに全部投稿済みです（start=0 で全部を材料にできます）")
    return unposted, render.skip_note(line.messages[0].position, unposted)


def draft_header_notes(
    store: ConversationStore, conversation_uuid: str, line: Line, through_index: int | None, skip_note: str | None
) -> str:
    """draft_blog_post の戻り値の先頭に並べる注記（飛ばした範囲・過去の投稿・枝分かれ）をまとめる。"""
    line_uuids = {pm.message.uuid for pm in line.messages}
    notes = [
        skip_note,
        render.render_posts_note(store.list_posts(conversation_uuid), line_uuids),
        render.render_branch_note(line.leaf_count, through_index),
    ]
    return "\n".join(n for n in notes if n)


@mcp_tool
async def draft_blog_post(
    conversation_uuid: str,
    through_index: ThroughIndex = None,
    start: Annotated[
        int | None,
        Field(ge=0, description="この index 以降だけを下書きの材料にする（省くと、まだ投稿していない部分から）"),
    ] = None,
    refresh: Annotated[bool, Field(description="保存済みの下書きがあっても作り直す")] = False,
) -> str:
    """Gemini でブログの下書き（タイトル・本文・カテゴリー）を作る。投稿はしない。

    下書きは保存され、2回目からは Gemini を呼ばない。
    投稿したら、下書きに使った範囲（戻り値の index a〜b）で record_blog_post を呼んで記録する。
    """
    with open_store() as store:
        line = store.get_line(conversation_uuid, through_index)
        if not line.messages:
            raise ValueError("メッセージがありません")
        start, skip_note = resolve_draft_start(store, line, start)
        header = draft_header_notes(store, conversation_uuid, line, through_index, skip_note)
    info, messages, transcript = load_transcript(conversation_uuid, through_index, start)
    material_chars = sum(len(pm.message.text) for pm in messages)
    header = "\n".join(n for n in (header, render.material_warning(material_chars, MIN_MATERIAL_CHARS)) if n)

    instructions = llm.load_prompt("blog")
    key = SummaryKey(
        conversation_uuid=conversation_uuid,
        first_message_uuid=messages[0].message.uuid,
        last_message_uuid=messages[-1].message.uuid,
        kind="blog_draft",
        model=DRAFT_MODEL,
        prompt_hash=prompt_hash(f"{TRANSCRIPT_VERSION}\n{instructions}"),
    )
    if not refresh:
        with open_store() as store:
            cached = store.find_summary(key)
        if cached is not None:
            draft = llm.BlogDraft.model_validate_json(cached.content)
            text = render.render_blog_draft(info, messages, cached, draft, cached=True, through_index=through_index)
            return f"{header}\n\n{text}" if header else text

    result = await llm.generate(make_model(DRAFT_MODEL), instructions, transcript, llm.BlogDraft)
    summary = Summary(key, result.output.model_dump_json(), result.input_tokens, result.output_tokens, now_db())
    with open_store() as store:
        store.save_summary(summary)
    text = render.render_blog_draft(info, messages, summary, result.output, cached=False, through_index=through_index)
    return f"{header}\n\n{text}" if header else text


@mcp_tool
def record_blog_post(
    conversation_uuid: str,
    service: Service,
    url: str,
    title: str,
    through_index: ThroughIndex = None,
    start: Annotated[
        int | None, Field(ge=0, description="記録する範囲の始まり（省くと、まだ投稿していない部分の始まり）")
    ] = None,
    end: Annotated[
        int | None, Field(ge=0, description="記録する範囲の終わり。含む（省くと選んだ枝の最後の index）")
    ] = None,
) -> str:
    """draft_blog_post で作った下書きを投稿したら、同じ範囲で呼んで記録する。投稿自体はしない。

    記録した範囲は、次回の draft_blog_post の既定の start や、list_conversations・search_messages の
    「投稿済み」表示に使われる。
    """
    with open_store() as store:
        info = find_conversation(store, conversation_uuid)
        line = store.get_line(conversation_uuid, through_index)
        if not line.messages:
            raise ValueError("メッセージがありません")
        positions = {pm.position for pm in line.messages}
        first, last = line.messages[0].position, line.messages[-1].position
        if start is None:
            unposted = store.unposted_start(line.messages)
            if unposted is None:
                raise ValueError("この枝はすでに全部投稿済みです。start を指定してください")
            start = unposted
        if end is None:
            end = last
        if start not in positions:
            raise ValueError(f"index={start} はこの枝にありません（{first}〜{last}）")
        if end not in positions:
            raise ValueError(f"index={end} はこの枝にありません（{first}〜{last}）")
        if start > end:
            raise ValueError(f"start（{start}）が end（{end}）より後ろです")
        message_uuids = [pm.message.uuid for pm in line.messages if start <= pm.position <= end]
        store.record_post(conversation_uuid, service, url, title, message_uuids)
    return (
        f"「{render.conversation_title(info.name, info.first_human_text)}」 conversation={conversation_uuid} の "
        f"index {start}〜{end}（この枝の {len(message_uuids)} 件）を {service} への投稿として記録しました: {url}"
    )


# --- MCP クライアント（Claude など）が自分で読んで書くための prompt ---

READING_STEPS = """\
会話 {conversation_uuid} を tsuduri の MCP ツールで読んでください。
- ファイルを読めるなら export_conversation{through} でファイルに書き出し、必要なところを読む
- 読めないなら get_messages{through} で start と count を進めながら、少しずつ読む
- 枝分かれした会話は1本の枝（既定は本線）だけを扱う。原文の全文を返事に貼り付けない
"""


def reading_steps(conversation_uuid: str, through_index: str | None) -> str:
    through = f"（through_index={through_index}）" if through_index else ""
    return READING_STEPS.format(conversation_uuid=conversation_uuid, through=through)


@mcp.prompt(title="会話を Claude が要約する")
def summarize_with_claude(conversation_uuid: str, through_index: str | None = None) -> str:
    """Gemini を使わず、MCP クライアント自身が会話を読んで要約する。"""
    return (
        reading_steps(conversation_uuid, through_index)
        + "\n読み終えたら、次の指示に従って要約してください。\n\n"
        + (llm.load_prompt("summary"))
    )


@mcp.prompt(title="会話から Claude がブログの下書きを書く")
def draft_blog_with_claude(conversation_uuid: str, through_index: str | None = None) -> str:
    """Gemini を使わず、MCP クライアント自身が会話を読んでブログの下書きを書く。投稿はしない。"""
    return (
        reading_steps(conversation_uuid, through_index)
        + "\n読み終えたら、次の指示に従って、タイトル・本文・カテゴリーを書いてください。投稿はしないでください。\n\n"
        + llm.load_prompt("blog")
    )


def main() -> None:
    mcp.run()  # 既定は stdio。stdout は通信に使われるので print してはいけない
