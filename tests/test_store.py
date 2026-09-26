import json

import pytest

from tsuduri_mcp.sources import ClaudeExportSource
from tsuduri_mcp.store import ConversationStore, connect

from claude_export import raw_conversation, raw_message

OLD = "2026-01-01T00:00:00.000000Z"
NEW = "2026-02-01T00:00:00.000000Z"


@pytest.fixture
def store():
    conn = connect(":memory:")
    yield ConversationStore(conn)
    conn.close()


def import_raw(store, tmp_path, conversations):
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(conversations, ensure_ascii=False), encoding="utf-8")
    return store.import_conversations(ClaudeExportSource(path).load())


def two_turns():
    return raw_conversation(
        "c1",
        [
            raw_message("m1", text="テストの質問"),
            raw_message("m2", sender="assistant", text="テストの答え", parent="m1"),
        ],
        updated_at=OLD,
    )


def test_imported_conversation_can_be_read_back(store, tmp_path):
    result = import_raw(store, tmp_path, [two_turns()])

    assert (result.added, result.messages_added) == (1, 2)
    conv = store.get_conversation("c1")
    assert conv.name == "テストの会話"
    assert [(m.sender, m.text) for m in conv.messages] == [("human", "テストの質問"), ("assistant", "テストの答え")]


def test_importing_same_export_twice_changes_nothing(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])

    result = import_raw(store, tmp_path, [two_turns()])

    assert (result.added, result.updated, result.unchanged, result.messages_added) == (0, 0, 1, 0)
    assert len(store.get_conversation("c1").messages) == 2


def test_continued_conversation_is_updated(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])
    continued = two_turns()
    continued["name"] = "テストの会話（続き）"
    continued["updated_at"] = NEW
    continued["chat_messages"].append(raw_message("m3", text="テストの追加の質問", parent="m2"))

    result = import_raw(store, tmp_path, [continued])

    assert (result.updated, result.messages_added) == (1, 1)
    conv = store.get_conversation("c1")
    assert conv.name == "テストの会話（続き）"
    assert [m.uuid for m in conv.messages] == ["m1", "m2", "m3"]


def test_older_export_does_not_overwrite(store, tmp_path):
    newer = two_turns()
    newer["updated_at"] = NEW
    newer["name"] = "テストの新しい名前"
    import_raw(store, tmp_path, [newer])
    older = two_turns()

    result = import_raw(store, tmp_path, [older])

    assert result.unchanged == 1
    assert store.get_conversation("c1").name == "テストの新しい名前"


def test_unknown_conversation_is_none(store):
    assert store.get_conversation("存在しないテストの会話") is None
