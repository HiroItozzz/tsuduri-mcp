from dataclasses import dataclass, field
from typing import Any


@dataclass
class Message:
    uuid: str
    sender: str  # "human" / "assistant"
    text: str  # 検索・要約に使う本文
    created_at: str
    updated_at: str
    parent_uuid: str | None = None
    raw_content: list[dict[str, Any]] = field(default_factory=list)  # 元データのブロックをそのまま保持する
    attachments: list[dict[str, Any]] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Conversation:
    uuid: str
    name: str
    summary: str
    created_at: str
    updated_at: str
    source: str  # 取り込み元のサービス（"claude" / "chatgpt"）
    messages: list[Message] = field(default_factory=list)
