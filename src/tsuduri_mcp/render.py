"""ツールの戻り値のテキストを組み立てる。

AI のコンテキストを節約するため、JSON ではなく短いテキストで返す（docs/design.md）。
"""

from collections.abc import Sequence
from dataclasses import dataclass

from .dates import db_to_local
from .llm import BlogDraft
from .store import ConversationSummary, MessageHit, MessageNote, Page, PositionedMessage, Summary

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
            f"\n--- conversation={hit.conversation_uuid} index={hit.position} {hit.sender} "
            f"{db_to_local(hit.created_at)} 「{conversation_title(hit.conversation_name)}」"
        )
        if not hit.on_main_line:
            heading += f" ［本線外。through_index={hit.position} でこの枝を読める］"
        lines.append(heading)
        lines.append(excerpt(hit.text, max_chars, keywords) or "（本文なし）")
    return "\n".join(lines)


def render_conversation_list(page: Page[ConversationSummary], offset: int) -> str:
    lines = [page_header(page.total, offset, len(page.items), "会話")]
    for c in page.items:
        lines.append(
            f"- {c.uuid} | {conversation_title(c.name, c.first_human_text)} | "
            f"作成 {db_to_local(c.created_at)} / 最終発言 {db_to_local(c.last_message_at)} | {c.message_count} 件"
            + (
                " | 本文なし（エクスポートに本文が含まれていない）"
                if c.message_count and not c.text_message_count
                else ""
            )
        )
    return "\n".join(lines)


@dataclass
class Scope:
    """get_messages / export で読む範囲。1本の線か、すべての枝か。"""

    messages: list[PositionedMessage]
    all_branches: bool
    leaf_count: int

    def describe(self, info: ConversationSummary) -> str:
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


def note_lines(pm: PositionedMessage) -> list[str]:
    """印のあるメッセージなら、見出しの次に出す説明の行を返す。"""
    return [_note_line(note) for note in pm.notes]


def branch_note(pm: PositionedMessage, scope: Scope) -> str:
    # 1本の線では親はいつも直前なので、すべての枝を並べたときだけ表示する
    if not scope.all_branches:
        return ""
    if pm.parent_position is None:
        return "新しい根（最初の発言の編集）" if pm.position != 0 else ""
    if pm.parent_position != pm.position - 1:
        return f"index={pm.parent_position} への返信（分岐）"
    return ""


def render_messages(info: ConversationSummary, scope: Scope, start: int, count: int, max_chars: int | None) -> str:
    candidates = [pm for pm in scope.messages if pm.position >= start]
    shown, rest = candidates[:count], candidates[count:]
    lines = [f"「{conversation_title(info.name, info.first_human_text)}」 conversation={info.uuid}"]
    lines.append(scope.describe(info))
    if not shown:
        lines.append(f"index={start} 以降のメッセージはありません。")
    else:
        head = f"index {shown[0].position}〜{shown[-1].position} を表示"
        if rest:
            head += f"（続きは start={rest[0].position}）"
        lines.append(head)
    for pm in shown:
        m = pm.message
        note = branch_note(pm, scope)
        lines.append(
            f"\n--- index={pm.position} {m.sender} {db_to_local(m.created_at)}" + (f" ↳ {note}" if note else "")
        )
        lines += note_lines(pm)
        text = m.text if max_chars is None else excerpt(m.text, max_chars)
        lines.append(text or _describe_empty(pm))
    return "\n".join(lines)


def render_markdown(info: ConversationSummary, scope: Scope, include_details: bool) -> str:
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
            f"## index={pm.position} {m.sender} {db_to_local(m.created_at)}" + (f"（{note}）" if note else ""),
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


def render_transcript(info: ConversationSummary, messages: Sequence[PositionedMessage]) -> str:
    """LLM に渡す会話ログ。本文だけで、見出しに index を付ける。"""
    lines = [f"# {conversation_title(info.name, info.first_human_text)}"]
    for pm in messages:
        m = pm.message
        lines += ["", f"### index={pm.position} {m.sender} {db_to_local(m.created_at)}"]
        lines += note_lines(pm)
        lines.append(m.text or _describe_empty(pm))
        lines += [f"[添付: {a.get('file_name', '')}]" for a in m.attachments]
    return "\n".join(lines)


def render_summary_header(
    info: ConversationSummary, messages: Sequence[PositionedMessage], summary: Summary, unit: str, cached: bool
) -> list[str]:
    key = summary.key
    lines = [
        f"「{conversation_title(info.name, info.first_human_text)}」 conversation={info.uuid}",
        f"index {messages[0].position}〜{messages[-1].position}（{len(messages)} 件）の{unit}",
        f"{key.model} / 入力 {summary.input_tokens} トークン・出力 {summary.output_tokens} トークン"
        f" / 作成 {db_to_local(summary.created_at)}",
    ]
    if cached:
        lines.append(f"保存済みの{unit}を返しました（作り直すときは refresh=true）")
    return lines


def render_summary(
    info: ConversationSummary, messages: Sequence[PositionedMessage], summary: Summary, cached: bool
) -> str:
    lines = render_summary_header(info, messages, summary, "要約", cached)
    return "\n".join(lines) + "\n\n" + summary.content


def render_blog_draft(
    info: ConversationSummary,
    messages: Sequence[PositionedMessage],
    summary: Summary,
    draft: BlogDraft,
    cached: bool,
) -> str:
    lines = render_summary_header(info, messages, summary, "下書き", cached)
    lines += [
        "",
        f"# {draft.title}",
        f"カテゴリー: {', '.join(draft.categories)}",
        "",
        draft.content,
        "",
        "（下書きです。まだ投稿していません）",
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


def _describe_empty(pm: PositionedMessage) -> str:
    m = pm.message
    parts = [f"添付 {len(m.attachments)} 件"] if m.attachments else []
    tools = [b.get("name", "") for b in m.raw_content if b.get("type") == "tool_use"]
    if tools:
        parts.append("ツール: " + ", ".join(tools))
    return "（本文なし" + ("。" + " / ".join(parts) if parts else "") + "）"
