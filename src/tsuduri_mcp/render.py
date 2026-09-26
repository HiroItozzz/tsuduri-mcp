"""ツールの戻り値のテキストを組み立てる。

AI のコンテキストを節約するため、JSON ではなく短いテキストで返す（docs/design.md）。
"""

from collections.abc import Sequence

from .dates import db_to_local
from .store import ConversationSummary, MessageHit, Page, PositionedMessage

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
        lines.append(
            f"\n--- conversation={hit.conversation_uuid} index={hit.position} {hit.sender} "
            f"{db_to_local(hit.created_at)} 「{conversation_title(hit.conversation_name)}」"
        )
        lines.append(excerpt(hit.text, max_chars, keywords) or "（本文なし）")
    return "\n".join(lines)


def render_conversation_list(page: Page[ConversationSummary], offset: int) -> str:
    lines = [page_header(page.total, offset, len(page.items), "会話")]
    for c in page.items:
        lines.append(
            f"- {c.uuid} | {conversation_title(c.name, c.first_human_text)} | "
            f"作成 {db_to_local(c.created_at)} / 更新 {db_to_local(c.updated_at)} | {c.message_count} 件"
        )
    return "\n".join(lines)


def render_messages(
    info: ConversationSummary, messages: Sequence[PositionedMessage], start: int, max_chars: int | None
) -> str:
    shown = len(messages)
    lines = [f"「{conversation_title(info.name, info.first_human_text)}」 conversation={info.uuid}"]
    if shown == 0:
        lines.append(f"全 {info.message_count} 件。index={start} 以降のメッセージはありません。")
    else:
        head = f"全 {info.message_count} 件中 index {start}〜{start + shown - 1}"
        if start + shown < info.message_count:
            head += f"（続きは start={start + shown}）"
        lines.append(head)
    for pm in messages:
        m = pm.message
        branch = ""
        if pm.parent_position is not None and pm.parent_position != pm.position - 1:
            branch = f" ↳ index={pm.parent_position} への返信（分岐）"
        lines.append(f"\n--- index={pm.position} {m.sender} {db_to_local(m.created_at)}{branch}")
        text = m.text if max_chars is None else excerpt(m.text, max_chars)
        lines.append(text or _describe_empty(pm))
    return "\n".join(lines)


def render_markdown(info: ConversationSummary, messages: Sequence[PositionedMessage], include_details: bool) -> str:
    """ファイルに書き出す用。本文は省略しない。"""
    lines = [
        f"# {conversation_title(info.name, info.first_human_text)}",
        "",
        f"- conversation: {info.uuid}",
        f"- 作成: {db_to_local(info.created_at)} / 更新: {db_to_local(info.updated_at)}",
        f"- メッセージ: {info.message_count} 件",
    ]
    for pm in messages:
        m = pm.message
        branch = ""
        if pm.parent_position is not None and pm.parent_position != pm.position - 1:
            branch = f"（index={pm.parent_position} への返信・分岐）"
        lines += ["", f"## index={pm.position} {m.sender} {db_to_local(m.created_at)}{branch}", ""]
        if include_details:
            lines += [_render_block(b) for b in m.raw_content]
        else:
            lines.append(m.text or _describe_empty(pm))
        for a in m.attachments:
            lines += ["", f"[添付: {a.get('file_name', '')}]", a.get("extracted_content", "")]
    return "\n".join(lines) + "\n"


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
