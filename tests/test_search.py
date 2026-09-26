import json

import pytest

from tsuduri_mcp.sources import ClaudeExportSource
from tsuduri_mcp.store import ConversationStore, connect

from claude_export import raw_conversation, raw_message


def at(day: int, hour: int = 0) -> str:
    return f"2026-09-{day:02d}T{hour:02d}:00:00.000000Z"


@pytest.fixture
def store(tmp_path):
    conversations = [
        raw_conversation(
            "c-python",
            [
                raw_message("p0", text="Python の参照について教えて", created_at=at(1)),
                raw_message(
                    "p1", sender="assistant", text="リストは参照が共有されます", parent="p0", created_at=at(1, 1)
                ),
                raw_message("p2", text="SQLite の全文検索も知りたい", parent="p1", created_at=at(2)),
            ],
            name="テストの Python の会話",
            updated_at=at(2),
        ),
        raw_conversation(
            "c-blog",
            [
                raw_message("b0", text="はてなブログに投稿したい", created_at=at(10)),
                raw_message(
                    "b1",
                    sender="assistant",
                    text="全文検索の結果をブログにしましょう",
                    parent="b0",
                    created_at=at(10, 1),
                ),
            ],
            name="",
            updated_at=at(20),
        ),
    ]
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(conversations, ensure_ascii=False), encoding="utf-8")
    conn = connect(":memory:")
    s = ConversationStore(conn)
    s.import_conversations(ClaudeExportSource(path).load())
    yield s
    conn.close()


def found(page):
    return [(h.conversation_uuid, h.position) for h in page.items]


# --- search_messages ---


def test_search_three_or_more_chars_uses_full_text_index(store):
    page = store.search_messages(["全文検索"])

    assert page.total == 2
    assert found(page) == [("c-blog", 1), ("c-python", 2)]  # 新しい順


def test_search_is_case_insensitive(store):
    assert found(store.search_messages(["python"])) == [("c-python", 0)]


def test_search_short_keyword(store):
    assert found(store.search_messages(["参照"])) == [("c-python", 1), ("c-python", 0)]


def test_search_all_and_any(store):
    assert found(store.search_messages(["全文検索", "ブログ"])) == [("c-blog", 1)]
    assert store.search_messages(["全文検索", "ブログ"], match="any").total == 3


def test_search_exclude(store):
    assert found(store.search_messages(["全文検索"], exclude=["ブログ"])) == [("c-python", 2)]


def test_search_filters(store):
    assert found(store.search_messages(["全文検索"], sender="assistant")) == [("c-blog", 1)]
    assert found(store.search_messages(["全文検索"], since=at(5))) == [("c-blog", 1)]
    assert found(store.search_messages(["全文検索"], until=at(5))) == [("c-python", 2)]
    assert found(store.search_messages(["全文検索"], conversation_uuid="c-python")) == [("c-python", 2)]


def test_search_paging(store):
    page = store.search_messages([], order="oldest", limit=2, offset=1)

    assert page.total == 5
    assert found(page) == [("c-python", 1), ("c-python", 2)]


def test_search_keyword_with_fts_syntax_is_literal(store):
    assert store.search_messages(['"AND OR*']).total == 0


# --- list_conversations ---


def test_list_by_period_includes_conversations_continued_in_it(store):
    page = store.list_conversations(since=at(15), until=at(25))

    assert [c.uuid for c in page.items] == ["c-blog"]  # 作成は 10 日だが 20 日に更新されている


def test_list_shows_first_human_text_and_counts(store):
    blog = store.list_conversations(title=None, since=at(15)).items[0]

    assert (blog.name, blog.first_human_text, blog.message_count) == ("", "はてなブログに投稿したい", 2)


def test_list_by_title(store):
    assert [c.uuid for c in store.list_conversations(title="python").items] == ["c-python"]


# --- get_messages ---


def test_get_messages_range(store):
    messages = store.get_messages("c-python", start=1, count=1)

    assert [(pm.position, pm.parent_position, pm.message.text) for pm in messages] == [
        (1, 0, "リストは参照が共有されます")
    ]


def test_short_keyword_with_like_wildcard_is_literal(store):
    assert store.search_messages(["%"]).total == 0
