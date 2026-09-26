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


def test_older_export_still_adds_its_own_messages(store, tmp_path):
    # 新しいエクスポート → 古いエクスポートの順で取り込んでも、古いほうにしかないメッセージは入る
    newer = two_turns()
    newer["updated_at"] = NEW
    newer["name"] = "テストの新しい名前"
    import_raw(store, tmp_path, [newer])
    older = two_turns()
    older["chat_messages"].append(raw_message("m3", text="古いエクスポートだけのメッセージ", parent="m2"))

    result = import_raw(store, tmp_path, [older])

    assert result.unchanged == 1
    assert result.messages_added == 1
    conv = store.get_conversation("c1")
    assert conv.name == "テストの新しい名前"  # 会話の名前は新しいほうのまま
    assert [m.uuid for m in conv.messages] == ["m1", "m2", "m3"]


def test_unknown_conversation_is_none(store):
    assert store.get_conversation("存在しないテストの会話") is None


# --- 消えた本文・メッセージの印 ---


def test_content_missing_note_is_added_when_content_disappears(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])
    dropped = two_turns()
    dropped["chat_messages"][0]["content"] = []

    result = import_raw(store, tmp_path, [dropped])

    assert result.notes_added == 1
    m1 = store.get_messages("c1")[0]
    assert m1.message.text == "テストの質問"  # 本文は残っている
    assert [n.kind for n in m1.notes] == ["content_missing"]


def test_message_missing_note_is_added_when_message_disappears(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])
    dropped = two_turns()
    dropped["chat_messages"] = dropped["chat_messages"][:1]  # m2 が消えたエクスポート

    result = import_raw(store, tmp_path, [dropped])

    assert result.notes_added == 1
    messages = store.get_messages("c1")
    m2 = next(pm for pm in messages if pm.message.uuid == "m2")
    assert [n.kind for n in m2.notes] == ["message_missing"]


def test_notes_are_not_duplicated_and_noticed_at_does_not_change(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])
    dropped = two_turns()
    dropped["chat_messages"][0]["content"] = []
    import_raw(store, tmp_path, [dropped])
    first_noticed_at = store.get_messages("c1")[0].notes[0].noticed_at

    result = import_raw(store, tmp_path, [dropped])

    assert result.notes_added == 0
    assert store.get_messages("c1")[0].notes[0].noticed_at == first_noticed_at


def test_content_missing_note_is_removed_when_content_comes_back(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])
    dropped = two_turns()
    dropped["chat_messages"][0]["content"] = []
    import_raw(store, tmp_path, [dropped])

    import_raw(store, tmp_path, [two_turns()])  # 本文つきで戻ってきた

    assert store.get_messages("c1")[0].notes == []


def test_message_missing_note_is_removed_when_message_comes_back(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])
    dropped = two_turns()
    dropped["chat_messages"] = dropped["chat_messages"][:1]
    import_raw(store, tmp_path, [dropped])

    import_raw(store, tmp_path, [two_turns()])  # 存在して戻ってきた

    messages = store.get_messages("c1")
    m2 = next(pm for pm in messages if pm.message.uuid == "m2")
    assert m2.notes == []


# --- ブログ投稿の記録 ---


def test_unposted_start_without_any_post_is_zero(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])

    assert store.unposted_start(store.get_line("c1").messages) == 0


def test_unposted_start_after_partial_post(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])
    store.record_post("c1", "hatena", "https://example.com/1", "タイトル", ["m1"])

    assert store.unposted_start(store.get_line("c1").messages) == 1


def test_unposted_start_when_fully_posted_is_none(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])
    store.record_post("c1", "hatena", "https://example.com/1", "タイトル", ["m1", "m2"])

    assert store.unposted_start(store.get_line("c1").messages) is None


def test_record_post_saves_service_url_title(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])

    post_id = store.record_post("c1", "hatena", "https://example.com/1", "タイトル", ["m1", "m2"])

    row = store.conn.execute("SELECT service, url, title FROM posts WHERE id = ?", (post_id,)).fetchone()
    assert (row["service"], row["url"], row["title"]) == ("hatena", "https://example.com/1", "タイトル")
    rows = store.conn.execute("SELECT message_uuid FROM post_messages WHERE post_id = ?", (post_id,))
    assert {r["message_uuid"] for r in rows} == {"m1", "m2"}
