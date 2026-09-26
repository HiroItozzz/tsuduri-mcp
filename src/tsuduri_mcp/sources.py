import json
import zipfile
from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .models import Conversation, Message


class ConversationSource(ABC):
    @abstractmethod
    def load(self) -> Iterator[Conversation]: ...


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
        if self.path.suffix == ".zip":
            with zipfile.ZipFile(self.path) as zf:
                name = next((n for n in zf.namelist() if Path(n).name == self.FILENAME), None)
                if name is None:
                    raise ValueError(f"{self.FILENAME} が zip の中にありません: {self.path}")
                return json.loads(zf.read(name))
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _to_conversation(self, raw: dict[str, Any]) -> Conversation:
        return Conversation(
            uuid=raw["uuid"],
            name=raw.get("name") or "",
            summary=raw.get("summary") or "",
            created_at=raw["created_at"],
            updated_at=raw["updated_at"],
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
