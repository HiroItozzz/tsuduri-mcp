import json
import zipfile

import pytest

from claude_export import raw_conversation, raw_message
from tsuduri_mcp.sources import ClaudeExportSource


def load(tmp_path, conversations):
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(conversations, ensure_ascii=False), encoding="utf-8")
    return list(ClaudeExportSource(path).load())


def test_text_is_built_from_text_blocks_only(tmp_path):
    content = [
        {"type": "thinking", "thinking": "テストの思考"},
        {"type": "text", "text": "前半"},
        {"type": "tool_use", "name": "web_search", "input": {"query": "テスト"}},
        {"type": "tool_result", "content": [{"type": "text", "text": "テストの検索結果"}]},
        {"type": "text", "text": "後半"},
    ]
    msg = raw_message("m1", sender="assistant", text="表示用の定型文", content=content)

    [conv] = load(tmp_path, [raw_conversation("c1", [msg])])

    message = conv.messages[0]
    assert message.text == "前半\n\n後半"
    assert message.raw_content == content  # 本文に入れないブロックも失われない


def test_first_message_has_no_parent(tmp_path):
    msgs = [raw_message("m1"), raw_message("m2", sender="assistant", parent="m1")]

    [conv] = load(tmp_path, [raw_conversation("c1", msgs)])

    assert [m.parent_uuid for m in conv.messages] == [None, "m1"]


def test_conversation_without_messages(tmp_path):
    [conv] = load(tmp_path, [raw_conversation("c1", [])])

    assert conv.messages == []


def test_reads_conversations_json_inside_zip(tmp_path):
    path = tmp_path / "conversations-000.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("conversations.json", json.dumps([raw_conversation("c1", [raw_message("m1")])]))

    [conv] = ClaudeExportSource(path).load()

    assert conv.uuid == "c1"


def test_zip_without_conversations_json_is_error(tmp_path):
    path = tmp_path / "projects-000.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("projects/テスト.json", "{}")

    with pytest.raises(ValueError):
        list(ClaudeExportSource(path).load())
