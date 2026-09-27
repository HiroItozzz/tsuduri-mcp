"""ツールの戻り値のテキストを組み立てる。

AI のコンテキストを節約するため、JSON ではなく短いテキストで返す（docs/design.md）。
"""

from collections.abc import Sequence
from dataclasses import dataclass

from .blog import FetchedArticle
from .dates import db_to_local
from .llm import BlogDraft
from .store import (
    ConversationInfo,
    MessageHit,
    MessageNote,
    Page,
    PostOverlap,
    PostRecord,
    StoredMessage,
    Summary,
)

UNTITLED = "（タイトルなし）"


def page_header(total: int, offset: int, shown: int, unit: str) -> str:
    if total == 0:
        return f"該当する{unit}はありません。"
    if shown == 0:
        return f"{total} 件ありますが、offset={offset} より後にはありません。"
    head = f"{total} 件中 {offset + 1}〜{offset + shown} 件目"
    if offset + shown < total:
        head += f"（続きは offset={offset + shown}）"
    return head


def excerpt(text: str, max_chars: int, keywords: Sequence[str] = ()) -> str:
    """長い本文は、最初に当たったキーワードのまわりを max_chars 文字だけ切り出す。"""
    if len(text) <= max_chars:
        return text
    lower = text.lower()
    hits = [i for k in keywords if k and (i := lower.find(k.lower())) >= 0]
    center = min(hits) if hits else 0
    start = max(0, min(center - max_chars // 3, len(text) - max_chars))
    end = start + max_chars
    return (
        ("…" if start > 0 else "")
        + text[start:end]
        + ("…" if end < len(text) else "")
        + f"\n（全 {len(text)} 文字のうち {start + 1}〜{end} 文字目）"
    )


def conversation_title(name: str, first_human_text: str = "") -> str:
    if name:
        return name
    if first_human_text:
        return f"{UNTITLED} {first_human_text[:40].replace(chr(10), ' ')}"
    return UNTITLED


def render_search(page: Page[MessageHit], offset: int, max_chars: int, keywords: Sequence[str]) -> str:
    lines = [page_header(page.total, offset, len(page.items), "メッセージ")]
    for hit in page.items:
        heading = (
            f"\n--- conversation={hit.conversation_uuid} index={hit.seq} {hit.sender} "
            f"{db_to_local(hit.created_at)} 「{conversation_title(hit.conversation_name)}」"
        )
        if not hit.on_main_line:
            heading += f" ［本線外。through_index={hit.seq} でこの枝を読める］"
        if hit.posted:
            heading += " ［投稿済み］"
        lines.append(heading)
        lines.append(excerpt(hit.text, max_chars, keywords) or "（本文なし）")
    return "\n".join(lines)


def render_conversation_list(page: Page[ConversationInfo], offset: int) -> str:
    lines = [page_header(page.total, offset, len(page.items), "会話")]
    for c in page.items:
        # メッセージが0件なら last_message_at は会話の updated_at（並べ替え用）なので、最終発言としては見せない
        last = (
            f"最終発言 {db_to_local(c.last_message_at)}"
            if c.message_count
            else f"最終発言 なし（会話の更新 {db_to_local(c.last_message_at)}）"
        )
        lines.append(
            f"- {c.uuid} | {conversation_title(c.name, c.first_human_text)} | "
            f"作成 {db_to_local(c.created_at)} / {last} | {c.message_count} 件"
            + (" | メッセージなし" if not c.message_count else "")
            + (
                " | 本文なし（エクスポートに本文が含まれていない）"
                if c.message_count and not c.text_message_count
                else ""
            )
            + (f" | 投稿 {c.post_count} 件・本線の未投稿 {c.main_line_unposted_count} 件" if c.post_count else "")
        )
    return "\n".join(lines)


@dataclass
class Scope:
    """get_messages / export で読む範囲。1本の線か、すべての枝か。"""

    messages: list[StoredMessage]
    all_branches: bool
    leaf_count: int

    def describe(self, info: ConversationInfo) -> str:
        if self.all_branches:
            return f"すべての枝の {len(self.messages)} 件"
        text = f"この線は {len(self.messages)} 件（会話全体 {info.message_count} 件"
        if self.leaf_count > 1:
            text += f"、枝 {self.leaf_count} 本。ほかの枝は through_index で選ぶ"
        return text + "）"


def _note_line(note: MessageNote) -> str:
    date = db_to_local(note.noticed_at).split(" ")[0]
    if note.kind == "content_missing":
        return f"（最新のエクスポートでは本文が消えている。{date} に確認）"
    if note.kind == "message_missing":
        return f"（最新のエクスポートにはこのメッセージがない。{date} に確認）"
    return f"（{note.kind}。{date} に確認）"


def note_lines(pm: StoredMessage) -> list[str]:
    """印のあるメッセージなら、見出しの次に出す説明の行を返す。"""
    return [_note_line(note) for note in pm.notes]


def branch_note(pm: StoredMessage, scope: Scope) -> str:
    # 1本の線では親はいつも直前なので、すべての枝を並べたときだけ表示する
    if not scope.all_branches:
        return ""
    if pm.parent_seq is None:
        return "新しい根（最初の発言の編集）" if pm.seq != 0 else ""
    if pm.parent_seq != pm.seq - 1:
        return f"index={pm.parent_seq} への返信（分岐）"
    return ""


def render_messages(info: ConversationInfo, scope: Scope, start: int, count: int, max_chars: int | None) -> str:
    candidates = [pm for pm in scope.messages if pm.seq >= start]
    shown, rest = candidates[:count], candidates[count:]
    lines = [f"「{conversation_title(info.name, info.first_human_text)}」 conversation={info.uuid}"]
    lines.append(scope.describe(info))
    if not shown:
        lines.append(f"index={start} 以降のメッセージはありません。")
    else:
        head = f"index {shown[0].seq}〜{shown[-1].seq} を表示"
        if rest:
            head += f"（続きは start={rest[0].seq}）"
        lines.append(head)
    for pm in shown:
        m = pm.message
        note = branch_note(pm, scope)
        lines.append(f"\n--- index={pm.seq} {m.sender} {db_to_local(m.created_at)}" + (f" ↳ {note}" if note else ""))
        lines += note_lines(pm)
        text = m.text if max_chars is None else excerpt(m.text, max_chars)
        lines.append(text or _describe_empty(pm))
    return "\n".join(lines)


def render_markdown(info: ConversationInfo, scope: Scope, include_details: bool) -> str:
    """ファイルに書き出す用。本文は省略しない。"""
    lines = [
        f"# {conversation_title(info.name, info.first_human_text)}",
        "",
        f"- conversation: {info.uuid}",
        f"- 作成: {db_to_local(info.created_at)} / 更新: {db_to_local(info.updated_at)}",
        f"- {scope.describe(info)}",
    ]
    for pm in scope.messages:
        m = pm.message
        note = branch_note(pm, scope)
        lines += [
            "",
            f"## index={pm.seq} {m.sender} {db_to_local(m.created_at)}" + (f"（{note}）" if note else ""),
        ]
        lines += note_lines(pm)
        lines.append("")
        if include_details:
            lines += [_render_block(b) for b in m.raw_content]
        else:
            lines.append(m.text or _describe_empty(pm))
        for a in m.attachments:
            lines += ["", f"[添付: {a.get('file_name', '')}]", a.get("extracted_content", "")]
    return "\n".join(lines) + "\n"


def render_transcript(info: ConversationInfo, messages: Sequence[StoredMessage]) -> str:
    """LLM に渡す会話ログ。本文だけで、見出しに index を付ける。"""
    lines = [f"# {conversation_title(info.name, info.first_human_text)}"]
    for pm in messages:
        m = pm.message
        lines += ["", f"### index={pm.seq} {m.sender} {db_to_local(m.created_at)}"]
        lines += note_lines(pm)
        lines.append(m.text or _describe_empty(pm))
        lines += [f"[添付: {a.get('file_name', '')}]" for a in m.attachments]
    return "\n".join(lines)


def format_cost(cost_usd: float | None, cached: bool) -> str:
    """generate_cached の結果を、料金の一言に変える。

    保存済みを使ったときは Gemini を呼んでいないので、料金がかかっていないことがわかるようにする。
    """
    if cached:
        return "保存済み（今回の料金なし）"
    if cost_usd is None:
        return "料金は不明"
    return f"約 ${cost_usd:.4f}"


def render_summary_header(
    info: ConversationInfo,
    messages: Sequence[StoredMessage],
    summary: Summary,
    unit: str,
    cached: bool,
    cost_usd: float | None = None,
    *,
    count_label: str | None = None,
) -> list[str]:
    """count_label を省くと「N 件」。会話全体の件数と混同しやすいところ（下書き）では明示する。

    cost_usd は Gemini を実際に呼んだとき（cached=False）だけ渡す。保存済みのときは None のままでよい。
    """
    key = summary.key
    count = count_label if count_label is not None else f"{len(messages)} 件"
    lines = [
        f"「{conversation_title(info.name, info.first_human_text)}」 conversation={info.uuid}",
        f"index {messages[0].seq}〜{messages[-1].seq}（{count}）の{unit}",
        f"{key.model} / 入力 {summary.input_tokens} トークン・出力 {summary.output_tokens} トークン"
        f" / 作成 {db_to_local(summary.created_at)} / {format_cost(cost_usd, cached)}",
    ]
    if cached:
        lines.append(f"保存済みの{unit}を返しました（作り直すときは refresh=true）")
    return lines


def render_summary(
    info: ConversationInfo,
    messages: Sequence[StoredMessage],
    summary: Summary,
    cached: bool,
    cost_usd: float | None = None,
) -> str:
    lines = render_summary_header(info, messages, summary, "要約", cached, cost_usd)
    return "\n".join(lines) + "\n\n" + summary.content


def skip_note(first_seq: int, start: int) -> str | None:
    """draft_blog_post が start を省いた既定値（未投稿の始まり）に決めたとき、飛ばした範囲の注記。

    投稿済みの部分を飛ばしていなければ（start が線の最初の index のままなら）None。
    start より前の全部が投稿済みとは限らない（途中だけ投稿していても、最後に投稿した次から始める）ため、
    範囲を言い切らず「index k より前に投稿済みの部分がある」とだけ伝える。
    """
    if start <= first_seq:
        return None
    return f"index {start} より前に投稿済みの部分があるので index {start} から下書きにした（全部使うなら start=0）"


def render_posts_note(posts: Sequence[PostRecord], line_uuids: set[str]) -> str | None:
    """この会話にすでに記録されている投稿を、draft_blog_post の材料と混同しないように知らせる。

    投稿に使ったメッセージが今読んでいる枝に全部あれば「この枝」、そうでなければ「別の枝」と添える。
    """
    if not posts:
        return None
    lines = ["この会話には投稿の記録があります:"]
    for p in posts:
        branch = "この枝" if p.message_uuids <= line_uuids else "別の枝"
        lines.append(f"- 「{p.title}」 {p.service} index {p.min_seq}〜{p.max_seq}（{branch}） post={p.id} {p.url}")
    return "\n".join(lines)


def render_duplicate_note(overlaps: Sequence[PostOverlap]) -> str | None:
    """記録しようとしている範囲に、すでに投稿の記録があるメッセージが含まれていれば知らせる。

    同じ範囲を別サービスに投稿することがあるので、止めずに知らせるだけにする。
    """
    if not overlaps:
        return None
    total = sum(o.count for o in overlaps)
    detail = "、".join(f"「{o.title}」（{o.service}）" for o in overlaps)
    return f"このうち {total} 件はすでに投稿の記録があります: {detail}"


def render_post_without_member_uri(p: PostRecord) -> str:
    """record_blog_post で入れた記録（member_uri がない）は、はてな側の今の状態を確かめられない。"""
    return (
        f"- 「{p.title}」 {p.service} index {p.min_seq}〜{p.max_seq} post={p.id} {p.url}\n"
        "  メンバー URI がないので確かめられない"
    )


def render_post_deleted(p: PostRecord) -> str:
    """はてなに GET して見つからなかった（削除されたらしい）ときの表示。記録は消さない。"""
    return (
        f"- 「{p.title}」 {p.service} index {p.min_seq}〜{p.max_seq} post={p.id} {p.url}\n"
        "  はてな側で削除されたようです。記録は残したまま"
    )


def render_post_status(p: PostRecord, fetched: FetchedArticle, url_updated: bool, include_content: bool) -> str:
    """はてなから読み出した記事の今の状態。予約中なら「予約（公開 <時刻>）」と表示する。"""
    status = f"予約（公開 {fetched.updated}）" if fetched.scheduled else ("下書き" if fetched.is_draft else "公開")
    lines = [
        f"- 「{fetched.title}」 {p.service} index {p.min_seq}〜{p.max_seq} post={p.id}",
        f"  {status} / URL: {fetched.url}" + ("（記録と違ったので更新しました）" if url_updated else ""),
        f"  編集: {fetched.edit_url}",
        f"  カテゴリー: {', '.join(fetched.categories) if fetched.categories else 'なし'}",
        # 下書きの updated は、取り消した予約の時刻などが残っていて紛らわしいので出さない
        f"  本文 {len(fetched.content)} 文字"
        + ("" if fetched.is_draft and not fetched.scheduled else f" / 投稿日時 {fetched.updated}")
        + f" / 最終編集 {fetched.edited}",
    ]
    if include_content:
        lines += ["", fetched.content]
    return "\n".join(lines)


def render_branch_note(leaf_count: int, through_index: int | None) -> str | None:
    """会話が枝分かれしていれば、いま読んでいるのがどの枝かを知らせる。"""
    if leaf_count <= 1:
        return None
    current = "本線" if through_index is None else f"through_index={through_index} の枝"
    return f"この会話は枝が {leaf_count} 本あります。いま読んでいる枝: {current}。別の枝は through_index で選べます"


def material_warning(material_chars: int, threshold: int) -> str | None:
    """下書きの材料（メッセージ本文の合計）が薄いときの注意。"""
    if material_chars >= threshold:
        return None
    return f"材料が少ない（{material_chars} 文字）。材料にない内容が混ざっていないか確かめてください"


def render_blog_draft(
    info: ConversationInfo,
    messages: Sequence[StoredMessage],
    summary: Summary,
    draft: BlogDraft,
    cached: bool,
    through_index: int | None = None,
    cost_usd: float | None = None,
) -> str:
    unit = "下書き"
    count_label = f"この枝の {len(messages)} 件"
    lines = render_summary_header(info, messages, summary, unit, cached, cost_usd, count_label=count_label)
    start, end = messages[0].seq, messages[-1].seq
    through_arg = f", through_index={through_index}" if through_index is not None else ""
    lines += [
        "",
        f"# {draft.title}",
        f"カテゴリー: {', '.join(draft.categories)}",
        "",
        draft.content,
        "",
        "（下書きです。まだ投稿していません）",
        f"この下書きをそのまま投稿するなら post_blog_article(start={start}, end={end}{through_arg}) だけでよい"
        "（保存済みの下書きが使われる。直すときは title・content・categories を渡す）。"
        "別の方法で投稿したときは、同じ範囲で record_blog_post を呼んで記録する",
    ]
    return "\n".join(lines)


def _render_block(block: dict) -> str:
    kind = block.get("type")
    if kind == "text":
        return block.get("text", "")
    if kind == "thinking":
        return "> [thinking]\n" + "\n".join("> " + line for line in (block.get("thinking") or "").splitlines())
    if kind == "tool_use":
        return f"[tool_use: {block.get('name', '')}]"
    if kind == "tool_result":
        return f"[tool_result: {block.get('name', '')}]"
    return f"[{kind}]"


def _describe_empty(pm: StoredMessage) -> str:
    m = pm.message
    parts = [f"添付 {len(m.attachments)} 件"] if m.attachments else []
    tools = [b.get("name", "") for b in m.raw_content if b.get("type") == "tool_use"]
    if tools:
        parts.append("ツール: " + ", ".join(tools))
    return "（本文なし" + ("。" + " / ".join(parts) if parts else "") + "）"
