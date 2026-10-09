import json
import zipfile

import pytest
from chatgpt_export import T0, gpt_conversation, gpt_message

from tsuduri_mcp import render
from tsuduri_mcp.sources import ChatGptExportSource, ClaudeExportSource
from tsuduri_mcp.store import ConversationStore, connect

from claude_export import raw_conversation, raw_message


def load(tmp_path, conversations):
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(conversations, ensure_ascii=False), encoding="utf-8")
    return list(ChatGptExportSource(path).load())


def test_conversation_fields(tmp_path):
    conv_raw = gpt_conversation("c1", [("root", None, None)], title="タイトル", update_time=T0 + 1.5)

    [conv] = load(tmp_path, [conv_raw])

    assert conv.uuid == "c1"
    assert conv.name == "タイトル"
    assert conv.summary == ""
    assert conv.created_at == "2026-01-01T00:00:00.000000Z"
    assert conv.updated_at == "2026-01-01T00:00:01.500000Z"
    assert conv.messages == []


def test_user_becomes_human_and_root_is_not_a_parent(tmp_path):
    nodes = [
        ("root", None, None),
        ("a", "root", gpt_message("a", "user", "質問")),
        ("b", "a", gpt_message("b", "assistant", "回答", create_time=T0 + 1)),
    ]

    [conv] = load(tmp_path, [gpt_conversation("c1", nodes)])

    assert [(m.uuid, m.sender, m.text, m.parent_uuid) for m in conv.messages] == [
        ("a", "human", "質問", None),
        ("b", "assistant", "回答", "a"),
    ]
    assert conv.messages[1].created_at == "2026-01-01T00:00:01.000000Z"
    assert conv.messages[1].updated_at == conv.messages[1].created_at  # update_time が null のとき


def test_dropped_nodes_are_skipped_and_children_relinked(tmp_path):
    nodes = [
        ("root", None, None),
        ("sys", "root", gpt_message("sys", "system", "システムの指示")),
        ("ctx", "sys", gpt_message("ctx", "user", "隠しの文脈", hidden=True)),
        ("a", "ctx", gpt_message("a", "user", create_time=T0 + 1)),
        ("tool", "a", gpt_message("tool", "tool", "検索結果", create_time=T0 + 2)),
        ("b", "tool", gpt_message("b", "assistant", create_time=T0 + 3)),
    ]

    [conv] = load(tmp_path, [gpt_conversation("c1", nodes)])

    assert [(m.uuid, m.parent_uuid) for m in conv.messages] == [("a", None), ("b", "a")]


def test_branches_are_all_kept_in_time_order(tmp_path):
    nodes = [
        ("root", None, None),
        ("a", "root", gpt_message("a", create_time=T0)),
        ("b1", "a", gpt_message("b1", "assistant", "最初の回答", create_time=T0 + 1)),
        ("b2", "a", gpt_message("b2", "assistant", "再生成した回答", create_time=T0 + 5)),
        ("c1", "b1", gpt_message("c1", create_time=T0 + 2)),
    ]

    [conv] = load(tmp_path, [gpt_conversation("conv", nodes)])

    assert [(m.uuid, m.parent_uuid) for m in conv.messages] == [("a", None), ("b1", "a"), ("c1", "b1"), ("b2", "a")]


def test_missing_create_time_inherits_from_parent(tmp_path):
    nodes = [
        ("root", None, None),
        ("a", "root", gpt_message("a", create_time=T0 + 7)),
        ("b", "a", gpt_message("b", "assistant", create_time=None)),
    ]

    [conv] = load(tmp_path, [gpt_conversation("c1", nodes)])

    assert [m.created_at for m in conv.messages] == ["2026-01-01T00:00:07.000000Z"] * 2


def test_text_uses_only_string_parts_of_text_content(tmp_path):
    image = {"content_type": "image_asset_pointer", "asset_pointer": "file-service://テスト"}
    multimodal = {"content_type": "multimodal_text", "parts": [image, "画像の説明", ""]}
    code = {"content_type": "code", "language": "python", "text": "print(1)"}
    nodes = [
        ("root", None, None),
        ("a", "root", gpt_message("a", content=multimodal)),
        ("b", "a", gpt_message("b", "assistant", content=code, create_time=T0 + 1)),
    ]

    [conv] = load(tmp_path, [gpt_conversation("c1", nodes)])

    assert [m.text for m in conv.messages] == ["画像の説明", ""]
    assert [m.raw_content for m in conv.messages] == [[multimodal], [code]]  # 本文に入れない中身も失われない


def test_reads_conversations_json_inside_zip(tmp_path):
    path = tmp_path / "chatgpt-export.zip"
    nodes = [("root", None, None), ("a", "root", gpt_message("a"))]
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("conversations.json", json.dumps([gpt_conversation("c1", nodes)]))
        zf.writestr("chat.html", "<html></html>")

    [conv] = ChatGptExportSource(path).load()

    assert [m.uuid for m in conv.messages] == ["a"]


def test_imported_branches_form_lines(tmp_path):
    nodes = [
        ("root", None, None),
        ("sys", "root", gpt_message("sys", "system", "")),
        ("a", "sys", gpt_message("a", create_time=T0)),
        ("b1", "a", gpt_message("b1", "assistant", "最初の回答", create_time=T0 + 1)),
        ("b2", "a", gpt_message("b2", "assistant", "再生成した回答", create_time=T0 + 5)),
    ]
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps([gpt_conversation("c1", nodes)], ensure_ascii=False), encoding="utf-8")
    store = ConversationStore(connect(":memory:"))
    store.import_conversations(ChatGptExportSource(path).load())

    main = store.get_line("c1")
    first = store.get_line("c1", through_index=1)

    assert [m.message.uuid for m in main.messages] == ["a", "b2"]
    assert [m.message.uuid for m in first.messages] == ["a", "b1"]
    assert main.leaf_count == 2


def import_both(tmp_path, chatgpt_uuid="g1"):
    """claude.ai の会話 c1 と ChatGPT の会話（uuid は chatgpt_uuid）を取り込んだ store を返す。"""
    claude_path = tmp_path / "claude.json"
    claude_path.write_text(json.dumps([raw_conversation("c1", [raw_message("m1", text="共通の話題")])]))
    gpt_path = tmp_path / "chatgpt.json"
    nodes = [("root", None, None), ("a", "root", gpt_message("a", text="共通の話題"))]
    gpt_path.write_text(json.dumps([gpt_conversation(chatgpt_uuid, nodes)]))
    store = ConversationStore(connect(":memory:"))
    store.import_conversations(ClaudeExportSource(claude_path).load())
    store.import_conversations(ChatGptExportSource(gpt_path).load())
    return store


def test_source_is_recorded_and_shown(tmp_path):
    store = import_both(tmp_path)

    sources = {c.uuid: c.source for c in store.list_conversations().items}
    listing = render.render_conversation_list(store.list_conversations(), 0)
    search = render.render_search(store.search_messages(["共通の話題"]), 0, 800, ["共通の話題"])

    assert sources == {"c1": "claude", "g1": "chatgpt"}
    assert [line for line in listing.splitlines() if "［ChatGPT］" in line] == [
        next(line for line in listing.splitlines() if line.startswith("- g1 "))
    ]
    assert [line for line in search.splitlines() if "［ChatGPT］" in line] == [
        next(line for line in search.splitlines() if "conversation=g1" in line)
    ]


def test_same_uuid_from_another_source_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="uuid が重なっています"):
        import_both(tmp_path, chatgpt_uuid="c1")
