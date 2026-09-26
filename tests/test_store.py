import json

import pytest

from tsuduri_mcp.sources import ClaudeExportSource
from tsuduri_mcp.store import MIGRATIONS, SCHEMA, ConversationStore, connect

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


def test_find_posted_overlap_is_empty_when_nothing_recorded(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])

    assert store.find_posted_overlap(["m1", "m2"]) == []


def test_find_posted_overlap_reports_count_per_post(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])
    store.record_post("c1", "hatena", "https://example.com/1", "はてなの記事", ["m1", "m2"])

    overlaps = store.find_posted_overlap(["m1", "m2"])

    assert len(overlaps) == 1
    assert (overlaps[0].service, overlaps[0].title, overlaps[0].count) == ("hatena", "はてなの記事", 2)


# --- LLM の呼び出し記録 ---


def test_record_llm_call_and_list_llm_calls_round_trip(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])

    store.record_llm_call("summary", "c1", "gemini-3-flash-preview", 100, 20, 0.0015, "2026-01-01T00:00:00Z")

    calls = store.list_llm_calls("c1")
    assert len(calls) == 1
    call = calls[0]
    assert (call.kind, call.model, call.input_tokens, call.output_tokens) == (
        "summary",
        "gemini-3-flash-preview",
        100,
        20,
    )
    assert call.cost_usd == 0.0015


def test_record_llm_call_allows_unknown_cost(store, tmp_path):
    import_raw(store, tmp_path, [two_turns()])

    store.record_llm_call("summary", "c1", "unknown-model", 10, 5, None, "2026-01-01T00:00:00Z")

    assert store.list_llm_calls("c1")[0].cost_usd is None


def test_list_llm_calls_filters_by_conversation(store, tmp_path):
    import_raw(store, tmp_path, [two_turns(), raw_conversation("c2", [raw_message("m3", text="別会話")])])

    store.record_llm_call("summary", "c1", "m", 1, 1, None, "2026-01-01T00:00:00Z")
    store.record_llm_call("summary", "c2", "m", 1, 1, None, "2026-01-01T00:00:00Z")

    assert [c.conversation_uuid for c in store.list_llm_calls("c1")] == ["c1"]


# --- DB 接続 ---


def test_connect_sets_wal_and_busy_timeout_for_file_db(tmp_path):
    conn = connect(tmp_path / "tsuduri.db")
    try:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert journal_mode == "wal"
        assert busy_timeout == 30000
    finally:
        conn.close()


def test_connect_does_not_set_wal_for_in_memory_db():
    conn = connect(":memory:")
    try:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert journal_mode != "wal"  # :memory: は WAL にならない
    finally:
        conn.close()


# --- DB の版（PRAGMA user_version） ---


def test_new_db_is_migrated_to_the_latest_version(tmp_path):
    conn = connect(tmp_path / "new.db")
    try:
        ConversationStore(conn)

        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    finally:
        conn.close()


def test_migration_adds_member_uri_column_and_keeps_existing_rows(tmp_path):
    path = tmp_path / "old.db"
    conn = connect(path)
    conn.executescript(SCHEMA)  # 版0の形（posts に member_uri 列がない）
    conn.execute(
        "INSERT INTO conversations (uuid, name, summary, created_at, updated_at) VALUES ('c1', '名前', '', 't', 't')"
    )
    conn.execute(
        """INSERT INTO posts (conversation_uuid, service, url, title, posted_at)
           VALUES ('c1', 'hatena', 'https://example.com/1', 'タイトル', 't')"""
    )
    conn.commit()
    conn.close()

    conn = connect(path)
    try:
        store = ConversationStore(conn)

        row = store.conn.execute("SELECT url, member_uri FROM posts").fetchone()
        assert row["url"] == "https://example.com/1"  # 既存の行は残っている
        assert row["member_uri"] is None  # 新しい列は NULL
        assert store.conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    finally:
        conn.close()


def test_reopening_a_migrated_db_does_not_fail(tmp_path):
    path = tmp_path / "old.db"
    conn = connect(path)
    ConversationStore(conn)  # 1回目で最新の版になる
    conn.close()

    conn = connect(path)
    try:
        ConversationStore(conn)  # 2回目に開いても、移行をやり直さず壊れない
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    finally:
        conn.close()
