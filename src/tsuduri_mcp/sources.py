import json
import zipfile
from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import Conversation, Message


class ConversationSource(ABC):
    @abstractmethod
    def load(self) -> Iterator[Conversation]: ...


def _read_export_json(path: Path, filename: str) -> Any:
    """filename の JSON を読む。path は JSON そのものか、それを含む zip。"""
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            name = next((n for n in zf.namelist() if Path(n).name == filename), None)
            if name is None:
                raise ValueError(f"{filename} が zip の中にありません: {path}")
            return json.loads(zf.read(name))
    return json.loads(path.read_text(encoding="utf-8"))


class ClaudeExportSource(ConversationSource):
    """claude.ai 公式エクスポートの conversations.json（または それを含む zip）を読む。"""

    FILENAME = "conversations.json"
    ROOT_PARENT_UUID = "00000000-0000-4000-8000-000000000000"  # 先頭メッセージの親として入っている値

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Iterator[Conversation]:
        for raw in self._read():
            yield self._to_conversation(raw)

    def _read(self) -> list[dict[str, Any]]:
        return _read_export_json(self.path, self.FILENAME)

    def _to_conversation(self, raw: dict[str, Any]) -> Conversation:
        return Conversation(
            uuid=raw["uuid"],
            name=raw.get("name") or "",
            summary=raw.get("summary") or "",
            created_at=raw["created_at"],
            updated_at=raw["updated_at"],
            source="claude",
            messages=[self._to_message(m) for m in raw.get("chat_messages") or []],
        )

    def _to_message(self, raw: dict[str, Any]) -> Message:
        content = raw.get("content") or []
        parent = raw.get("parent_message_uuid")
        return Message(
            uuid=raw["uuid"],
            sender=raw["sender"],
            # message の text 欄には画面表示用の定型文が混ざるため、text ブロックだけから本文を作る
            text="\n\n".join(b["text"] for b in content if b.get("type") == "text" and b.get("text")),
            created_at=raw["created_at"],
            updated_at=raw["updated_at"],
            parent_uuid=None if parent == self.ROOT_PARENT_UUID else parent,
            raw_content=content,
            attachments=raw.get("attachments") or [],
            files=raw.get("files") or [],
        )


def _iso_from_epoch(t: float) -> str:
    """Unix 秒を、DB でそろえている 2026-01-01T00:00:00.000000Z の形にする。"""
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class ChatGptExportSource(ConversationSource):
    """ChatGPT のエクスポートの conversations.json（または それを含む zip）を読む。

    実物のエクスポートではまだ確かめていない（docs/design.md の「ChatGPT の取り込み」）。
    会話のメッセージは mapping（ノード id → ノード）の木になっている。user / assistant の
    見えるメッセージだけを取り込み、落としたノード（根・system・tool・隠しメッセージ）の子は、
    残る祖先につなぎ直す。
    """

    FILENAME = "conversations.json"
    ROLES = {"user": "human", "assistant": "assistant"}

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Iterator[Conversation]:
        for raw in _read_export_json(self.path, self.FILENAME):
            yield self._to_conversation(raw)

    def _to_conversation(self, raw: dict[str, Any]) -> Conversation:
        created_at = _iso_from_epoch(raw["create_time"])
        update_time = raw.get("update_time")
        return Conversation(
            uuid=raw.get("id") or raw["conversation_id"],
            name=raw.get("title") or "",
            summary="",
            created_at=created_at,
            updated_at=_iso_from_epoch(update_time) if update_time is not None else created_at,
            source="chatgpt",
            messages=self._to_messages(raw.get("mapping") or {}, created_at),
        )

    def _to_messages(self, mapping: dict[str, Any], conv_created_at: str) -> list[Message]:
        # 根から深さ優先でたどる。親は先に処理されるので、つなぎ先と時刻の補いがそのまま決まる
        roots = [nid for nid, node in mapping.items() if node.get("parent") not in mapping]
        stack: list[tuple[str, str | None, str]] = [(nid, None, conv_created_at) for nid in reversed(roots)]
        messages: list[Message] = []
        seen: set[str] = set()
        while stack:
            nid, parent, inherited_at = stack.pop()
            if nid in seen or nid not in mapping:
                continue
            seen.add(nid)
            node = mapping[nid]
            message = self._to_message(node.get("message"), parent, inherited_at)
            if message is not None:
                messages.append(message)
                parent, inherited_at = message.uuid, message.created_at
            for child in reversed(node.get("children") or []):
                stack.append((child, parent, inherited_at))
        # seq はこの並びで振られる。時刻順にし、同じ時刻なら木の順を保つ（sorted は安定）
        return sorted(messages, key=lambda m: m.created_at)

    def _to_message(self, raw: dict[str, Any] | None, parent: str | None, inherited_at: str) -> Message | None:
        if raw is None:
            return None
        sender = self.ROLES.get((raw.get("author") or {}).get("role"))
        metadata = raw.get("metadata") or {}
        if sender is None or metadata.get("is_visually_hidden_from_conversation"):
            return None
        content = raw.get("content") or {}
        # parts には文字列のほか、画像などの dict が入る。本文には文字列だけを使う
        is_text = content.get("content_type") in ("text", "multimodal_text")
        parts = (content.get("parts") or []) if is_text else []
        create_time, update_time = raw.get("create_time"), raw.get("update_time")
        created_at = _iso_from_epoch(create_time) if create_time is not None else inherited_at
        return Message(
            uuid=raw["id"],
            sender=sender,
            text="\n\n".join(p for p in parts if isinstance(p, str) and p),
            created_at=created_at,
            updated_at=_iso_from_epoch(update_time) if update_time is not None else created_at,
            parent_uuid=parent,
            raw_content=[content],
            attachments=metadata.get("attachments") or [],
        )
