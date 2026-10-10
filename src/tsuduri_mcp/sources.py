import json
import re
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


# ChatGPT の本文に埋め込まれている印（私用領域の文字）。画面では引用のリンクや強調に置き換わる
_MARKER_SPAN = re.compile("\ue200([^\ue201]*)\ue201")  # \ue200 種類 \ue202 引数 \ue202 … \ue201
_MARKER_CHAR = re.compile("[\ue200-\ue2ff]")  # 残りの区切り（引用された範囲を囲む \ue203〜\ue206 など）


def _replace_marker(match: re.Match[str]) -> str:
    """印を、画面に出る文字に置き換える。引用（cite など）は画面では記号になるだけなので消す。"""
    kind, *args = match.group(1).split("\ue202")
    if kind in ("entity", "product_entity") and args:
        # 引数は ["種類", "表示名", …] の JSON
        try:
            value = json.loads(args[0])
        except ValueError:
            return ""
        return value[1] if isinstance(value, list) and len(value) > 1 and isinstance(value[1], str) else ""
    if kind in ("link_title", "navlist", "video") and args:
        return args[0]  # 最初の引数が表示される題名。残りは turn0search0 などの参照
    return ""


def _strip_markers(text: str) -> str:
    return _MARKER_CHAR.sub("", _MARKER_SPAN.sub(_replace_marker, text))


class ChatGptExportSource(ConversationSource):
    """ChatGPT のエクスポートの conversations-000.json などを読む（docs/design.md の「ChatGPT の取り込み」）。

    path はエクスポートの zip、それを展開したフォルダー、または JSON のファイル1つ。
    会話のメッセージは mapping（ノード id → ノード）の木で、ノードは親（parent）だけを持つ。
    user / assistant の見えるメッセージだけを取り込み、取り込まないノード（根・system・tool・
    隠しメッセージ・カスタム指示）の子は、残る祖先につなぎ直す。
    推論の途中経過（thoughts / reasoning_recap）は、続く assistant の応答の raw_content に入れて1つにまとめる。
    """

    FILE_PATTERN = re.compile(r"conversations(-\d+)?\.json")  # 大きいエクスポートは 100 会話ずつに分かれる
    ROLES = {"user": "human", "assistant": "assistant"}
    SKIPPED_CONTENT_TYPES = {"user_editable_context"}  # カスタム指示。発言ではない
    REASONING_CONTENT_TYPES = {"thoughts", "reasoning_recap"}

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Iterator[Conversation]:
        for data in self._read_files():
            for raw in data:
                yield self._to_conversation(raw)

    def _read_files(self) -> Iterator[list[dict[str, Any]]]:
        """会話の JSON をファイルごとに読む（全部を一度にメモリに載せないため）。"""
        if self.path.suffix == ".zip":
            with zipfile.ZipFile(self.path) as zf:
                names = sorted(n for n in zf.namelist() if self.FILE_PATTERN.fullmatch(Path(n).name))
                if not names:
                    raise ValueError(f"conversations-000.json などが zip の中にありません: {self.path}")
                for name in names:
                    yield json.loads(zf.read(name))
        elif self.path.is_dir():
            paths = sorted(p for p in self.path.iterdir() if self.FILE_PATTERN.fullmatch(p.name))
            if not paths:
                raise ValueError(f"conversations-000.json などがフォルダーにありません: {self.path}")
            for path in paths:
                yield json.loads(path.read_text(encoding="utf-8"))
        else:
            yield json.loads(self.path.read_text(encoding="utf-8"))

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
        # 子は parent から組み立てる（ノードに children がないエクスポートがある）。親が mapping にないノードが根
        children: dict[str | None, list[str]] = {}
        for nid, node in mapping.items():
            parent = node.get("parent")
            children.setdefault(parent if parent in mapping else None, []).append(nid)

        # 根から深さ優先でたどる。親は先に処理されるので、つなぎ先と時刻の補いがそのまま決まる。
        # carried は、まとめる途中の推論の content（続く応答の raw_content の先頭に入れる）。
        # order_at は並べ替えに使う時刻で、親の order_at より前にならない（下記）
        Item = tuple[str, str | None, str, list[dict[str, Any]]]
        stack: list[Item] = [(nid, None, conv_created_at, []) for nid in reversed(children.get(None, []))]
        ordered: list[tuple[str, Message]] = []
        while stack:
            nid, parent, parent_order_at, carried = stack.pop()
            raw = mapping[nid].get("message")
            kids = children.get(nid, [])
            if self._folds_into_child(raw, kids, mapping):
                stack.append((kids[0], parent, parent_order_at, [*carried, raw["content"]]))
                continue
            message = self._to_message(raw, parent, parent_order_at, carried)
            if message is not None:
                # 応答の時刻が直前の質問より少し（実データで最大1分）前のことがある。時刻だけで並べると
                # 子の seq が親より前になるので、親より前には置かない
                order_at = max(message.created_at, parent_order_at)
                ordered.append((order_at, message))
                parent, parent_order_at, carried = message.uuid, order_at, []
            for child in reversed(kids):
                stack.append((child, parent, parent_order_at, carried))
        # seq はこの並びで振られる。時刻順にし、同じ時刻なら木の順（親が先）を保つ（sorted は安定）
        return [m for _, m in sorted(ordered, key=lambda item: item[0])]

    def _folds_into_child(self, raw: dict[str, Any] | None, kids: list[str], mapping: dict[str, Any]) -> bool:
        """推論の途中経過で、続きが assistant の1本道なら、続きのメッセージにまとめる。

        子がない（途中で止まった）・枝分かれしている・続きが assistant でないときは、まとめずに1件のメッセージにする。
        """
        if raw is None or self._role(raw) != "assistant" or len(kids) != 1:
            return False
        if (raw.get("content") or {}).get("content_type") not in self.REASONING_CONTENT_TYPES:
            return False
        child = mapping[kids[0]].get("message")
        return child is not None and self._role(child) == "assistant"

    @staticmethod
    def _role(raw: dict[str, Any]) -> str | None:
        return (raw.get("author") or {}).get("role")

    def _to_message(
        self, raw: dict[str, Any] | None, parent: str | None, fallback_at: str, carried: list[dict[str, Any]]
    ) -> Message | None:
        if raw is None:
            return None
        sender = self.ROLES.get(self._role(raw) or "")
        metadata = raw.get("metadata") or {}
        content = raw.get("content") or {}
        if (
            sender is None
            or metadata.get("is_visually_hidden_from_conversation")
            or content.get("content_type") in self.SKIPPED_CONTENT_TYPES
        ):
            return None
        create_time, update_time = raw.get("create_time"), raw.get("update_time")
        # 時刻がないときは親の時刻で補う
        created_at = _iso_from_epoch(create_time) if create_time is not None else fallback_at
        return Message(
            uuid=raw["id"],
            sender=sender,
            text=self._text(content),
            created_at=created_at,
            updated_at=_iso_from_epoch(update_time) if update_time is not None else created_at,
            parent_uuid=parent,
            raw_content=[*carried, content],
            attachments=metadata.get("attachments") or [],
        )

    @staticmethod
    def _text(content: dict[str, Any]) -> str:
        """text / multimodal_text の parts から本文を作る。

        使うのは文字列と音声の書き起こしだけ（画像などは使わない）。文字列からは印を取り除く。
        """
        if content.get("content_type") not in ("text", "multimodal_text"):
            return ""
        pieces = []
        for part in content.get("parts") or []:
            if isinstance(part, str):
                pieces.append(_strip_markers(part))
            elif isinstance(part, dict) and part.get("content_type") == "audio_transcription":
                text = part.get("text")
                if isinstance(text, str):
                    pieces.append(text)
        return "\n\n".join(p for p in pieces if p)
