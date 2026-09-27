"""根が複数ある会話（最初の発言を編集した会話。実データに 39 件ある）で、ツールの既定の範囲を確かめる。

本線はいちばん新しい根の線だけで、index が 0 から始まらない。
"""

import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tsuduri_mcp import server
from tsuduri_mcp.sources import ClaudeExportSource
from tsuduri_mcp.store import ConversationStore, connect, default_db_path

from claude_export import raw_conversation, raw_message
from test_draft import FakeGemini
from test_post_blog_article import FakePoster


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    #  a0 ─ a1   （index 0, 1。元の発言）
    #  b0 ─ b1   （index 2, 3。a0 を編集して作った別の根。こちらが新しいので本線）
    msgs = [
        raw_message("a0", text="最初のテストの質問", created_at="2026-01-01T00:00:00.000000Z"),
        raw_message(
            "a1", sender="assistant", text="最初のテストの答え", parent="a0", created_at="2026-01-01T00:01:00.000000Z"
        ),
        raw_message("b0", text="言い直したテストの質問", created_at="2026-01-01T00:02:00.000000Z"),
        raw_message(
            "b1",
            sender="assistant",
            text="言い直した質問への答え",
            parent="b0",
            created_at="2026-01-01T00:03:00.000000Z",
        ),
    ]
    src = tmp_path / "conversations.json"
    src.write_text(json.dumps([raw_conversation("c1", msgs)], ensure_ascii=False), encoding="utf-8")
    path = tmp_path / "tsuduri.db"
    conn = connect(path)
    ConversationStore(conn).import_conversations(ClaudeExportSource(src).load())
    conn.close()
    monkeypatch.setenv("TSUDURI_DB", str(path))


@pytest.fixture
def gemini(monkeypatch):
    fake = FakeGemini()
    monkeypatch.setattr(server, "make_model", fake.model)
    return fake


@pytest.fixture
def fake_poster(monkeypatch):
    fake = FakePoster()
    monkeypatch.setattr(server, "make_poster", lambda service: fake)
    return fake


def record(**kwargs):
    return server.record_blog_post("c1", "hatena", "https://example.com/1", "タイトル", **kwargs)


def draft(**kwargs):
    return asyncio.run(server.draft_blog_post("c1", **kwargs))


def list_posts():
    conn = connect(default_db_path())
    try:
        return ConversationStore(conn).list_posts("c1")
    finally:
        conn.close()


# --- record_blog_post ---


def test_record_default_range_is_the_newest_root():
    assert "index 2〜3（この枝の 2 件）" in record()


def test_record_through_old_root_uses_that_root():
    assert "index 0〜1（この枝の 2 件）" in record(through_index=0)


def test_record_on_old_root_does_not_change_the_default_of_the_main_line():
    record(through_index=0)

    assert "index 2〜3" in record()


def test_record_start_on_the_other_root_is_error():
    with pytest.raises(ToolError, match=r"index=0 はこの枝にありません（2〜3）"):
        record(start=0)


# --- draft_blog_post ---


def test_draft_default_starts_at_the_newest_root_without_skip_note(gemini):
    result = draft()

    assert "index 2〜3（この枝の 2 件）の下書き" in result
    assert "投稿済みなので" not in result  # 線の最初から始めるので、飛ばした範囲はない
    assert "この会話は枝が 2 本あります" in result


def test_draft_default_skips_the_posted_part_of_the_main_line(gemini):
    record(end=2)

    result = draft()

    assert "index 3〜3（この枝の 1 件）の下書き" in result
    assert "index 2〜2 は投稿済みなので index 3 から" in result


def test_draft_is_not_affected_by_a_post_on_the_old_root(gemini):
    record(through_index=0)

    result = draft()

    assert "index 2〜3（この枝の 2 件）の下書き" in result
    assert "（別の枝）" in result  # 古い根への投稿は、別の枝として知らせる


# --- post_blog_article ---


def test_post_default_range_is_the_newest_root(fake_poster):
    result = asyncio.run(server.post_blog_article("c1", title="タイトル", content="本文"))

    assert "index 2〜3（この枝の 2 件）" in result
    [posted] = list_posts()
    assert (posted.min_seq, posted.max_seq) == (2, 3)
    assert posted.message_uuids == {"b0", "b1"}
