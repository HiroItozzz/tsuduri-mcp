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


def test_short_keyword_escapes_like_wildcards(tmp_path):
    # "_" は LIKE のワイルドカード（任意の1文字）。エスケープしていないと、どんな1文字にも当たってしまう
    conversations = [
        raw_conversation(
            "c",
            [
                raw_message("m0", text="foo_x bar", created_at=at(1)),  # 文字どおりの "_x" を含む
                raw_message("m1", sender="assistant", text="foo9x bar", parent="m0", created_at=at(1, 1)),
            ],
        )
    ]
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(conversations, ensure_ascii=False), encoding="utf-8")
    conn = connect(":memory:")
    s = ConversationStore(conn)
    s.import_conversations(ClaudeExportSource(path).load())

    assert found(s.search_messages(["_x"])) == [("c", 0)]  # m1 の "9x" には当たらない
    conn.close()


def test_short_keyword_escapes_backslash(tmp_path):
    conversations = [raw_conversation("c", [raw_message("m0", text="path\\x here", created_at=at(1))])]
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(conversations, ensure_ascii=False), encoding="utf-8")
    conn = connect(":memory:")
    s = ConversationStore(conn)
    s.import_conversations(ClaudeExportSource(path).load())

    assert found(s.search_messages(["\\x"])) == [("c", 0)]
    conn.close()


# --- list_conversations ---


def test_list_by_period_uses_message_times(store):
    # c-python は 1 日に作られ 2 日に続きを話した。c-blog は発言が 10 日で、会話の更新日だけが 20 日
    assert [c.uuid for c in store.list_conversations(since=at(2), until=at(3)).items] == ["c-python"]
    assert [c.uuid for c in store.list_conversations(since=at(5), until=at(15)).items] == ["c-blog"]
    assert store.list_conversations(since=at(15), until=at(25)).total == 0


def test_list_with_only_since(store):
    assert [c.uuid for c in store.list_conversations(since=at(5)).items] == ["c-blog"]


def test_list_with_only_until(store):
    assert [c.uuid for c in store.list_conversations(until=at(3)).items] == ["c-python"]


def test_list_shows_first_human_text_and_counts(store):
    blog = store.list_conversations(uuid="c-blog").items[0]

    assert (blog.name, blog.first_human_text, blog.message_count, blog.text_message_count) == (
        "",
        "はてなブログに投稿したい",
        2,
        2,
    )


def test_list_by_title(store):
    assert [c.uuid for c in store.list_conversations(title="python").items] == ["c-python"]


def test_list_orders_by_last_message_not_conversation_updated_at(tmp_path):
    # renamed-only はタイトル変更だけで updated_at が新しくなった会話。発言は really-active より古い
    conversations = [
        raw_conversation("renamed-only", [raw_message("r0", text="質問", created_at=at(5))], updated_at=at(30)),
        raw_conversation("really-active", [raw_message("q0", text="質問", created_at=at(8))], updated_at=at(8)),
    ]
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(conversations, ensure_ascii=False), encoding="utf-8")
    conn = connect(":memory:")
    s = ConversationStore(conn)
    s.import_conversations(ClaudeExportSource(path).load())

    page = s.list_conversations()

    assert [c.uuid for c in page.items] == ["really-active", "renamed-only"]
    assert page.items[0].last_message_at == at(8)
    conn.close()


# --- get_messages ---


def test_get_messages_range(store):
    messages = store.get_messages("c-python", start=1, count=1)

    assert [(pm.position, pm.parent_position, pm.message.text) for pm in messages] == [
        (1, 0, "リストは参照が共有されます")
    ]


def test_short_keyword_with_like_wildcard_is_literal(store):
    assert store.search_messages(["%"]).total == 0


# --- get_line ---


@pytest.fixture
def branchy(tmp_path):
    #  m0 ─ m1 ─ m2 ─ m3           （m2 が再生成されて m2b に分岐）
    #            └ m2b ─ m3b ─ m4b （こちらが新しい）
    msgs = [
        raw_message("m0", text="テストの質問", created_at=at(1, 0)),
        raw_message("m1", sender="assistant", text="テストの答え", parent="m0", created_at=at(1, 1)),
        raw_message("m2", text="テストの続き", parent="m1", created_at=at(1, 2)),
        raw_message("m3", sender="assistant", text="古い枝の答え", parent="m2", created_at=at(1, 3)),
        raw_message("m2b", text="言い直したテストの続き", parent="m1", created_at=at(1, 4)),
        raw_message("m3b", sender="assistant", text="新しい枝の答え", parent="m2b", created_at=at(1, 5)),
        raw_message("m4b", text="新しい枝のさらに続き", parent="m3b", created_at=at(1, 6)),
    ]
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps([raw_conversation("c", msgs)], ensure_ascii=False), encoding="utf-8")
    conn = connect(":memory:")
    s = ConversationStore(conn)
    s.import_conversations(ClaudeExportSource(path).load())
    yield s
    conn.close()


def uuids(line):
    return [pm.message.uuid for pm in line.messages]


def test_line_defaults_to_newest_branch(branchy):
    line = branchy.get_line("c")

    assert uuids(line) == ["m0", "m1", "m2b", "m3b", "m4b"]
    assert line.leaf_count == 2


def test_line_through_old_branch_follows_it_to_the_end(branchy):
    assert uuids(branchy.get_line("c", through_index=2)) == ["m0", "m1", "m2", "m3"]


def test_line_through_common_part_takes_newest_continuation(branchy):
    assert uuids(branchy.get_line("c", through_index=1)) == ["m0", "m1", "m2b", "m3b", "m4b"]


def test_line_unknown_index_is_error(branchy):
    with pytest.raises(ValueError):
        branchy.get_line("c", through_index=99)


def test_line_of_empty_conversation(store):
    assert store.get_line("存在しないテストの会話").messages == []


def test_line_with_two_roots_takes_the_newest_root(tmp_path):
    #  a0 ─ a1                    （元の発言）
    #  b0 ─ b1                    （a0 を編集して作った、別の根。こちらが新しい）
    msgs = [
        raw_message("a0", text="最初の質問", created_at=at(1, 0)),
        raw_message("a1", sender="assistant", text="最初の答え", parent="a0", created_at=at(1, 1)),
        raw_message("b0", text="言い直した質問", created_at=at(1, 2)),
        raw_message("b1", sender="assistant", text="新しい根の答え", parent="b0", created_at=at(1, 3)),
    ]
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps([raw_conversation("c", msgs)], ensure_ascii=False), encoding="utf-8")
    conn = connect(":memory:")
    s = ConversationStore(conn)
    s.import_conversations(ClaudeExportSource(path).load())

    assert uuids(s.get_line("c")) == ["b0", "b1"]
    # 投稿がなければ、未投稿の始まりは 0 ではなく線の最初の index
    assert s.unposted_start(s.get_line("c").messages) == 2
    conn.close()


# --- search_messages の on_main_line / main_line_only ---


def test_search_marks_messages_off_the_main_line(branchy):
    # 「テストの続き」は古い枝の m2 と、本線の m2b（言い直したテストの続き）の両方に当たる
    page = branchy.search_messages(["テストの続き"])

    assert [(h.position, h.on_main_line) for h in page.items] == [(4, True), (2, False)]


def test_search_main_line_only_drops_other_branches(branchy):
    page = branchy.search_messages(["テストの続き"], main_line_only=True)

    assert found(page) == [("c", 4)]
    assert page.total == 1


def test_reimporting_extended_old_branch_swaps_main_line(branchy, tmp_path):
    # 古い枝（m3 の続き）に m5 を足した新しいエクスポートを取り込む。m5 がいちばん新しくなるので本線が入れ替わる
    msgs = [
        raw_message("m0", text="テストの質問", created_at=at(1, 0)),
        raw_message("m1", sender="assistant", text="テストの答え", parent="m0", created_at=at(1, 1)),
        raw_message("m2", text="テストの続き", parent="m1", created_at=at(1, 2)),
        raw_message("m3", sender="assistant", text="古い枝の答え", parent="m2", created_at=at(1, 3)),
        raw_message("m2b", text="言い直したテストの続き", parent="m1", created_at=at(1, 4)),
        raw_message("m3b", sender="assistant", text="新しい枝の答え", parent="m2b", created_at=at(1, 5)),
        raw_message("m4b", text="新しい枝のさらに続き", parent="m3b", created_at=at(1, 6)),
        raw_message("m5", text="古い枝への追加発言", parent="m3", created_at=at(1, 7)),
    ]
    path = tmp_path / "conversations.json"
    path.write_text(
        json.dumps([raw_conversation("c", msgs, updated_at=at(1, 7))], ensure_ascii=False), encoding="utf-8"
    )

    branchy.import_conversations(ClaudeExportSource(path).load())

    assert uuids(branchy.get_line("c")) == ["m0", "m1", "m2", "m3", "m5"]
    page = branchy.search_messages(["テストの続き"])
    assert [(h.position, h.on_main_line) for h in page.items] == [(4, False), (2, True)]  # 印が入れ替わっている


# --- ブログ投稿の記録（枝ごとに扱う） ---


def test_unposted_start_is_scoped_to_the_line(branchy):
    # 古い枝（m0, m1, m2, m3）を投稿済みにする
    old_line = branchy.get_line("c", through_index=2)
    branchy.record_post("c", "hatena", "https://example.com/1", "タイトル", uuids(old_line))

    # 古い枝はもう投稿済み。共通の親（m0, m1）を持つ本線は、m2b（index=4）から未投稿になる
    assert branchy.unposted_start(branchy.get_line("c", through_index=2).messages) is None
    assert branchy.unposted_start(branchy.get_line("c").messages) == 4


def test_search_marks_posted_messages(branchy):
    branchy.record_post("c", "hatena", "https://example.com/1", "タイトル", ["m0", "m1"])

    page = branchy.search_messages(["テストの答え"])

    assert [(h.position, h.posted) for h in page.items] == [(1, True)]


def test_list_conversations_shows_post_and_unposted_counts(branchy):
    branchy.record_post("c", "hatena", "https://example.com/1", "タイトル", ["m0", "m1"])

    info = branchy.list_conversations(uuid="c").items[0]

    assert (info.post_count, info.main_line_unposted_count) == (1, 3)  # 本線は5件、投稿済みは m0, m1 の2件


def test_list_conversations_without_post_has_zero_counts(store):
    info = store.list_conversations(uuid="c-python").items[0]

    assert (info.post_count, info.main_line_unposted_count) == (0, 3)
