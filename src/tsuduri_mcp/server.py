import functools
import hashlib
import inspect
import logging
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field
from pydantic_ai.models import Model

from . import blog, lines, llm, log, render
from .dates import now_db, since_to_db, until_to_db
from .store import (
    ConversationInfo,
    ConversationStore,
    Line,
    PositionedMessage,
    Summary,
    SummaryKey,
    connect,
    default_db_path,
)

logger = logging.getLogger(f"{log.LOGGER_NAME}.server")

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
- はてなブログへ投稿するときは post_blog_article を使う（既定は下書き投稿）。投稿した範囲は自動で記録される
- post_blog_article を使わずに投稿したときは、record_blog_post で範囲を記録する。次の draft_blog_post は続きから作れる
- 記録した投稿が今どうなっているか（下書きのままか、削除されていないか）は check_blog_posts で確かめる
"""

MAX_LIMIT = 100
EXPORT_DIR = Path(tempfile.gettempdir()) / "tsuduri-mcp"
MAX_INPUT_CHARS = 600_000  # いちばん長い会話の本線でも約 36 万文字（2026-09 時点）
MIN_MATERIAL_CHARS = 1000  # 下書きの材料（本文の合計）がこれ未満なら「材料が薄い」と注意する

SUMMARY_MODEL = llm.DEFAULT_GEMINI_MODEL
DRAFT_MODEL = llm.DEFAULT_GEMINI_MODEL
make_model: Callable[[str], Model] = llm.gemini  # テストでは通信しないモデルに差し替える


def default_poster(service: str) -> blog.BlogPoster:
    if service == "hatena":
        return blog.HatenaPoster.from_env()
    raise ValueError(f"{service} への投稿にはまだ対応していません")


make_poster: Callable[[str], blog.BlogPoster] = default_poster  # テストでは通信しないポスターに差し替える

mcp = MCPServer("tsuduri", instructions=INSTRUCTIONS)

# MCPServer は ToolError 以外の例外の文言を消してクライアントに返すので、入口で ToolError に変える
CAUGHT_EXCEPTIONS = (ValueError, FileNotFoundError, RuntimeError)

# ログに残す引数だけを選ぶ。会話の本文・検索キーワード・認証情報などは載せない
LOGGED_TOOL_ARGS = ("conversation_uuid", "through_index", "start", "end", "service", "publish", "refresh")


def _describe_args(fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """ログに残す引数だけを "key=value" の並びにする。引数の対応付けに失敗しても例外にはしない。"""
    try:
        bound = inspect.signature(fn).bind(*args, **kwargs)
        bound.apply_defaults()
        values = bound.arguments
    except TypeError:
        values = kwargs
    return " ".join(f"{name}={values[name]!r}" for name in LOGGED_TOOL_ARGS if name in values)


def mcp_tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """`@mcp.tool(structured_output=False)` の代わりに使う、全ツール共通のデコレータ。

    引数の名前・説明・既定値（入力スキーマ）は元の関数のまま変わらない
    （functools.wraps で __wrapped__ を残し、シグネチャの取得はそちらに流れる）。
    ツール名・一部の引数・かかった時間・成否をログに書く。
    """
    name = getattr(fn, "__name__", repr(fn))  # Callable には __name__ がない場合もあるので getattr で逃がす
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            arg_desc = _describe_args(fn, args, kwargs)
            started = time.monotonic()
            try:
                result = await fn(*args, **kwargs)
            except CAUGHT_EXCEPTIONS as e:
                logger.exception("%s(%s) 失敗 %.3fs", name, arg_desc, time.monotonic() - started)
                raise ToolError(str(e)) from e
            except Exception:
                logger.exception("%s(%s) 失敗 %.3fs", name, arg_desc, time.monotonic() - started)
                raise
            logger.info("%s(%s) 成功 %.3fs", name, arg_desc, time.monotonic() - started)
            return result
    else:

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            arg_desc = _describe_args(fn, args, kwargs)
            started = time.monotonic()
            try:
                result = fn(*args, **kwargs)
            except CAUGHT_EXCEPTIONS as e:
                logger.exception("%s(%s) 失敗 %.3fs", name, arg_desc, time.monotonic() - started)
                raise ToolError(str(e)) from e
            except Exception:
                logger.exception("%s(%s) 失敗 %.3fs", name, arg_desc, time.monotonic() - started)
                raise
            logger.info("%s(%s) 成功 %.3fs", name, arg_desc, time.monotonic() - started)
            return result

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


def find_conversation(store: ConversationStore, conversation_uuid: str) -> ConversationInfo:
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
    store: ConversationStore, conversation_uuid: str, line: Line, start: int
) -> tuple[ConversationInfo, list[PositionedMessage], str]:
    """すでに開いた store と、すでに選んだ線（line）から、LLM に渡す会話ログを作る。

    line.messages のうち position >= start のものだけを使う。
    """
    info = find_conversation(store, conversation_uuid)
    messages = [pm for pm in line.messages if pm.position >= start]
    if not messages:
        raise ValueError(f"index={start} 以降のメッセージがありません")
    transcript = render.render_transcript(info, messages)
    if len(transcript) > MAX_INPUT_CHARS:
        raise ValueError(f"会話が長すぎます（{len(transcript)} 文字）。start で範囲をしぼってください")
    return info, messages, transcript


@dataclass
class CachedGeneration[T]:
    """generate_cached の戻り値。あとでログや料金の計算を足すときも、ここに足していく。"""

    output: T
    summary: Summary
    cached: bool  # 保存済みのものを使ったら True
    cost_usd: float | None  # 実際に Gemini を呼んだときの料金（USD）。cached なら None


async def generate_cached[T](
    key: SummaryKey,
    instructions: str,
    transcript: str,
    output_type: type[T],
    model_name: str,
    refresh: bool,
    dump: Callable[[T], str],
    load: Callable[[str], T],
) -> CachedGeneration[T]:
    """summarize_conversation と draft_blog_post に共通の、Gemini を呼んで保存する流れ。

    dump・load は、出力と summaries.content（文字列）との変換。
    Gemini を呼んでいる間（await）は DB を開いたままにしない。
    実際に Gemini を呼んだときだけ、料金と一緒に llm_calls に1行記録する。
    """
    if not refresh:
        with open_store() as store:
            cached = store.find_summary(key)
        if cached is not None:
            logger.info(
                "generate_cached kind=%s model=%s conversation=%s range=%s..%s cached=True "
                "input_tokens=%d output_tokens=%d",
                key.kind,
                key.model,
                key.conversation_uuid,
                key.first_message_uuid,
                key.last_message_uuid,
                cached.input_tokens,
                cached.output_tokens,
            )
            return CachedGeneration(load(cached.content), cached, True, None)

    result = await llm.generate(make_model(model_name), instructions, transcript, output_type)
    created_at = now_db()
    summary = Summary(key, dump(result.output), result.input_tokens, result.output_tokens, created_at)
    with open_store() as store:
        store.save_summary(summary)
        store.record_llm_call(
            key.kind,
            key.conversation_uuid,
            model_name,
            result.input_tokens,
            result.output_tokens,
            result.cost_usd,
            created_at,
        )
    logger.info(
        "generate_cached kind=%s model=%s conversation=%s range=%s..%s cached=False "
        "input_tokens=%d output_tokens=%d cost_usd=%s",
        key.kind,
        key.model,
        key.conversation_uuid,
        key.first_message_uuid,
        key.last_message_uuid,
        result.input_tokens,
        result.output_tokens,
        result.cost_usd,
    )
    return CachedGeneration(result.output, summary, False, result.cost_usd)


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
    with open_store() as store:
        line = store.get_line(conversation_uuid, through_index)
        info, messages, transcript = load_transcript(store, conversation_uuid, line, start)

    instructions = llm.load_prompt("summary")
    key = SummaryKey(
        conversation_uuid=conversation_uuid,
        first_message_uuid=messages[0].message.uuid,
        last_message_uuid=messages[-1].message.uuid,
        kind="summary",
        model=SUMMARY_MODEL,
        prompt_hash=prompt_hash(f"{TRANSCRIPT_VERSION}\n{instructions}"),
    )
    generated = await generate_cached(key, instructions, transcript, str, SUMMARY_MODEL, refresh, dump=str, load=str)
    return render.render_summary(
        info, messages, generated.summary, cached=generated.cached, cost_usd=generated.cost_usd
    )


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
    投稿は post_blog_article に、戻り値の index a〜b をそのまま渡す（投稿した範囲も記録される）。
    """
    with open_store() as store:
        line = store.get_line(conversation_uuid, through_index)
        if not line.messages:
            raise ValueError("メッセージがありません")
        start, skip_note = resolve_draft_start(store, line, start)
        header = draft_header_notes(store, conversation_uuid, line, through_index, skip_note)
        info, messages, transcript = load_transcript(store, conversation_uuid, line, start)
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
    generated = await generate_cached(
        key,
        instructions,
        transcript,
        llm.BlogDraft,
        DRAFT_MODEL,
        refresh,
        dump=llm.BlogDraft.model_dump_json,
        load=llm.BlogDraft.model_validate_json,
    )
    text = render.render_blog_draft(
        info,
        messages,
        generated.summary,
        generated.output,
        cached=generated.cached,
        through_index=through_index,
        cost_usd=generated.cost_usd,
    )
    return f"{header}\n\n{text}" if header else text


@dataclass
class ResolvedRange:
    """record_blog_post と post_blog_article で共通の、記録・投稿する範囲。"""

    line: Line
    start: int
    end: int
    message_uuids: list[str]


RangeStart = Annotated[int | None, Field(ge=0, description="範囲の始まり（省くと、まだ投稿していない部分の始まり）")]
RangeEnd = Annotated[int | None, Field(ge=0, description="範囲の終わり。含む（省くと選んだ枝の最後の index）")]


def resolve_post_range(
    store: ConversationStore, conversation_uuid: str, through_index: int | None, start: int | None, end: int | None
) -> ResolvedRange:
    """record_blog_post・post_blog_article で範囲（start〜end）を決めて検証する。

    投稿してから範囲エラーにならないよう、post_blog_article はこれを投稿の前に呼ぶ。
    """
    line = store.get_line(conversation_uuid, through_index)
    if not line.messages:
        raise ValueError("メッセージがありません")
    if start is None:
        unposted = store.unposted_start(line.messages)
        if unposted is None:
            raise ValueError("この枝はすでに全部投稿済みです。start を指定してください")
        start = unposted
    start, end = lines.check_range([pm.position for pm in line.messages], start, end)
    message_uuids = [pm.message.uuid for pm in line.messages if start <= pm.position <= end]
    return ResolvedRange(line, start, end, message_uuids)


@mcp_tool
def record_blog_post(
    conversation_uuid: str,
    service: Service,
    url: str,
    title: str,
    through_index: ThroughIndex = None,
    start: RangeStart = None,
    end: RangeEnd = None,
) -> str:
    """post_blog_article を使わずに投稿したときだけ呼ぶ。投稿自体はしない。

    記録した範囲は、次回の draft_blog_post の既定の start や、list_conversations・search_messages の
    「投稿済み」表示に使われる。
    """
    with open_store() as store:
        info = find_conversation(store, conversation_uuid)
        resolved = resolve_post_range(store, conversation_uuid, through_index, start, end)
        duplicate_note = render.render_duplicate_note(store.find_posted_overlap(resolved.message_uuids))
        # 記録に失敗しても手がかりが残るように、DB に書く前にログへ残す（post_blog_article と同じ理由）
        logger.info(
            "record_blog_post service=%s url=%s conversation=%s index=%d-%d",
            service,
            url,
            conversation_uuid,
            resolved.start,
            resolved.end,
        )
        store.record_post(conversation_uuid, service, url, title, resolved.message_uuids)
    result = (
        f"「{render.conversation_title(info.name, info.first_human_text)}」 conversation={conversation_uuid} の "
        f"index {resolved.start}〜{resolved.end}（この枝の {len(resolved.message_uuids)} 件）を "
        f"{service} への投稿として記録しました: {url}"
    )
    return f"{duplicate_note}\n\n{result}" if duplicate_note else result


@mcp_tool
async def post_blog_article(
    conversation_uuid: str,
    title: Annotated[str | None, Field(description="タイトル（省くと、この範囲の保存済みの下書きのもの）")] = None,
    content: Annotated[
        str | None, Field(description="本文（Markdown）。省くと、この範囲の保存済みの下書きのもの")
    ] = None,
    categories: Annotated[
        list[str] | None, Field(description="カテゴリー（省くと、下書きを使うときはそのカテゴリー。ほかはなし）")
    ] = None,
    service: Annotated[Literal["hatena"], Field(description="投稿先")] = "hatena",
    through_index: ThroughIndex = None,
    start: RangeStart = None,
    end: RangeEnd = None,
    publish: Annotated[bool, Field(description="true なら公開。false（既定）なら下書きとして投稿")] = False,
) -> str:
    """ブログに投稿し、投稿した範囲を記録する。

    draft_blog_post の下書きは、同じ範囲（start・end）を渡すだけで、保存済みのものがそのまま投稿される。
    直したいときや draft_blog_with_claude で書いたときは、title・content・categories を渡す
    （渡した項目だけ差し替わる）。
    既定は下書きとして投稿する（publish=true で公開）。範囲や下書きは投稿する前に確かめる。
    このツールを使わずに投稿したときの記録は record_blog_post で行う。
    """
    with open_store() as store:
        resolved = resolve_post_range(store, conversation_uuid, through_index, start, end)
        duplicate_note = render.render_duplicate_note(store.find_posted_overlap(resolved.message_uuids))
        saved = None
        if title is None or content is None:
            saved = store.find_latest_draft(resolved.message_uuids[0], resolved.message_uuids[-1])
            if saved is None:
                raise ValueError(
                    f"index {resolved.start}〜{resolved.end} の保存済みの下書きがありません。"
                    "draft_blog_post で同じ範囲の下書きを作るか、title と content を渡してください（投稿していません）"
                )
    if saved is not None:
        draft = llm.BlogDraft.model_validate_json(saved)
        title = draft.title if title is None else title
        content = draft.content if content is None else content
        categories = draft.categories if categories is None else categories
    assert title is not None and content is not None
    poster = make_poster(service)
    article = blog.BlogArticle(title=title, content=content, categories=categories or [])
    result = await poster.post(article, draft=not publish)
    # 記録に失敗しても URL が残るように、DB に書く前にログへ残す
    logger.info(
        "post_blog_article service=%s url=%s edit_url=%s draft=%s conversation=%s index=%d-%d",
        service,
        result.url,
        result.edit_url,
        not publish,
        conversation_uuid,
        resolved.start,
        resolved.end,
    )
    try:
        with open_store() as store:
            store.record_post(conversation_uuid, service, result.url, title, resolved.message_uuids, result.member_uri)
    except Exception as e:  # 投稿は済んでいるので、どんな失敗でも URL を返す
        through_arg = f", through_index={through_index}" if through_index is not None else ""
        raise RuntimeError(
            f"投稿は済んでいます: {result.url}（編集: {result.edit_url}）。"
            f"index {resolved.start}〜{resolved.end} の記録に失敗しました（{e}）。"
            f"record_blog_post(start={resolved.start}, end={resolved.end}{through_arg}, "
            f'service="{service}", url="{result.url}", title={title!r}) で記録してください。'
            "post_blog_article をやり直すと二重投稿になります"
        ) from e
    if result.is_draft:
        head = f"下書きとして投稿しました: {result.url}（公開するまで外からは見えない。編集: {result.edit_url}）。"
    else:
        head = f"公開しました: {result.url}（編集: {result.edit_url}）。"
    body = (
        head + f"conversation={conversation_uuid} の index {resolved.start}〜{resolved.end}"
        f"（この枝の {len(resolved.message_uuids)} 件）を記録しました"
    )
    return f"{duplicate_note}\n\n{body}" if duplicate_note else body


@mcp_tool
async def check_blog_posts(
    conversation_uuid: str,
    include_content: Annotated[bool, Field(description="true なら本文も全文表示する")] = False,
) -> str:
    """記録した投稿の、はてな側での今の状態を読み出す。

    下書きは編集するたびに URL が変わるので、記録と違えば記録の URL を今の URL に更新する。
    はてな側で見つからない（削除されたらしい）ときも記録は消さず、そのことだけ表示する。
    record_blog_post で記録したもの（member_uri がない）は確かめられない。
    """
    with open_store() as store:
        info = find_conversation(store, conversation_uuid)
        posts = store.list_posts(conversation_uuid)
    if not posts:
        return f"「{render.conversation_title(info.name, info.first_human_text)}」に投稿の記録はありません"

    blocks = []
    for p in posts:
        if p.member_uri is None:
            blocks.append(render.render_post_without_member_uri(p))
            continue
        poster = make_poster(p.service)
        fetched = await poster.get(p.member_uri)
        if fetched is None:
            blocks.append(render.render_post_deleted(p))
            continue
        url_updated = fetched.url != p.url
        if url_updated:
            with open_store() as store:
                store.update_post_url(p.id, fetched.url)
        blocks.append(render.render_post_status(p, fetched, url_updated, include_content))

    title = render.conversation_title(info.name, info.first_human_text)
    header = f"「{title}」 conversation={conversation_uuid} の投稿 {len(posts)} 件"
    return header + "\n\n" + "\n\n".join(blocks)


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
    log.setup_logging()
    mcp.run()  # 既定は stdio。stdout は通信に使われるので print してはいけない
