"""MCP ツールのテスト。ツールは普通の関数としても呼べるので、戻り値のテキストを確かめる。"""

import json
from pathlib import Path

import pytest

from tsuduri_mcp import server
from tsuduri_mcp.sources import ClaudeExportSource
from tsuduri_mcp.store import ConversationStore, connect

from claude_export import raw_conversation, raw_message

LONG = "前置き" * 500 + "つづりちゃん" + "後書き" * 500


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    conversations = [
        raw_conversation(
            "c1",
            [
                raw_message("m0", text="テストの質問です"),
                raw_message("m1", sender="assistant", text=LONG, parent="m0"),
                raw_message("m2", text="別の聞き方をしたテストの質問", parent="m0"),
            ],
            name="つづりちゃんのテスト会話",
        )
    ]
    src = tmp_path / "conversations.json"
    src.write_text(json.dumps(conversations, ensure_ascii=False), encoding="utf-8")
    path = tmp_path / "tsuduri.db"
    conn = connect(path)
    ConversationStore(conn).import_conversations(ClaudeExportSource(src).load())
    conn.close()
    monkeypatch.setenv("TSUDURI_DB", str(path))
    monkeypatch.setattr(server, "EXPORT_DIR", tmp_path / "export")


def test_search_returns_conversation_and_index_with_excerpt():
    result = server.search_messages(["つづりちゃん"], max_chars=100)

    assert "conversation=c1 index=1" in result
    assert "つづりちゃん" in result  # 長い本文でもキーワードのまわりが残る
    assert len(result) < 500


def test_list_conversations():
    assert "c1 | つづりちゃんのテスト会話" in server.list_conversations()


def test_get_messages_defaults_to_newest_branch():
    result = server.get_messages("c1")

    assert "index=0" in result
    assert "index=2" in result
    assert "index=1" not in result  # 古い枝
    assert "枝 2 本" in result


def test_get_messages_through_index_reads_other_branch():
    result = server.get_messages("c1", through_index=1, max_chars=100)

    assert "index=1" in result
    assert "index=2" not in result


def test_get_messages_paging_on_a_line():
    result = server.get_messages("c1", start=0, count=1)

    assert "続きは start=2" in result  # 線の上の次は index=2


def test_get_messages_all_branches_marks_branch():
    result = server.get_messages("c1", all_branches=True, max_chars=100)

    assert "index=0 への返信（分岐）" in result


def test_export_writes_full_text_to_file():
    result = server.export_conversation("c1", through_index=1)

    path = Path(result.split(" に書き出しました")[0])
    assert LONG in path.read_text(encoding="utf-8")
    assert LONG not in result


def test_unknown_conversation_is_error():
    with pytest.raises(ValueError):
        server.get_messages("存在しないテストの会話")


def test_missing_db_is_error(monkeypatch, tmp_path):
    monkeypatch.setenv("TSUDURI_DB", str(tmp_path / "ない.db"))

    with pytest.raises(FileNotFoundError):
        server.list_conversations()


def test_prompts_tell_client_how_to_read():
    summary = server.summarize_with_claude("c1")
    blog = server.draft_blog_with_claude("c1", through_index="1")

    assert "c1" in summary
    assert "index" in summary  # 要約の形式（prompts/summary.md）が入っている
    assert "through_index=1" in blog
    assert "投稿はしないで" in blog


def test_list_marks_conversation_without_text(tmp_path, monkeypatch):
    hollow = raw_conversation("c-hollow", [raw_message("h0", text="", content=[])], name="")
    src = tmp_path / "hollow.json"
    src.write_text(json.dumps([hollow], ensure_ascii=False), encoding="utf-8")
    path = tmp_path / "hollow.db"
    conn = connect(path)
    ConversationStore(conn).import_conversations(ClaudeExportSource(src).load())
    conn.close()
    monkeypatch.setenv("TSUDURI_DB", str(path))

    assert "本文なし" in server.list_conversations()
