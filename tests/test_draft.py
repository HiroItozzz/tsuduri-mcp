import asyncio
import json

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from tsuduri_mcp import server
from tsuduri_mcp.sources import ClaudeExportSource
from tsuduri_mcp.store import ConversationStore, connect

from claude_export import raw_conversation, raw_message


class FakeGemini:
    """Gemini の代わり。呼ばれた回数を記録して、決まった下書き（または要約）を返す。

    summarize_conversation（output_type=str）と draft_blog_post（output_type=BlogDraft）の
    両方から呼ばれるので、構造化出力が求められているかどうかで返し方を変える。
    """

    def __init__(self):
        self.calls = 0

    def model(self, model_name: str) -> FunctionModel:
        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            self.calls += 1
            if info.output_tools:
                args = {
                    "title": f"テストの下書き {self.calls} 回目",
                    "content": "本文だよ",
                    "categories": ["カテゴリA", "カテゴリB"],
                }
                return ModelResponse(parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=args)])
            return ModelResponse(parts=[TextPart(f"テストの要約 {self.calls} 回目")])

        return FunctionModel(respond, model_name=model_name)


@pytest.fixture
def gemini(tmp_path, monkeypatch):
    msgs = [
        raw_message("m0", text="最初のテストの質問"),
        raw_message("m1", sender="assistant", text="最初のテストの答え", parent="m0"),
        raw_message("m2", text="二つ目のテストの質問", parent="m1"),
    ]
    src = tmp_path / "conversations.json"
    src.write_text(json.dumps([raw_conversation("c1", msgs)], ensure_ascii=False), encoding="utf-8")
    path = tmp_path / "tsuduri.db"
    conn = connect(path)
    ConversationStore(conn).import_conversations(ClaudeExportSource(src).load())
    conn.close()
    monkeypatch.setenv("TSUDURI_DB", str(path))
    fake = FakeGemini()
    monkeypatch.setattr(server, "make_model", fake.model)
    return fake


def draft(**kwargs):
    return asyncio.run(server.draft_blog_post("c1", **kwargs))


def record_post(message_uuids, service="hatena", url="https://example.com/1", title="旧タイトル"):
    conn = connect(server.default_db_path())
    ConversationStore(conn).record_post("c1", service, url, title, message_uuids)
    conn.close()


def test_draft_is_made_from_the_line(gemini):
    result = draft()

    assert "テストの下書き 1 回目" in result
    assert "本文だよ" in result
    assert "カテゴリー: カテゴリA, カテゴリB" in result
    assert "まだ投稿していません" in result


def test_second_call_uses_saved_draft(gemini):
    draft()

    result = draft()

    assert gemini.calls == 1
    assert "テストの下書き 1 回目" in result
    assert "保存済み" in result


def test_refresh_calls_gemini_again(gemini):
    draft()

    result = draft(refresh=True)

    assert gemini.calls == 2
    assert "テストの下書き 2 回目" in result


def test_summary_and_draft_are_saved_separately(gemini):
    summary_result = asyncio.run(server.summarize_conversation("c1"))
    draft_result = draft()
    assert gemini.calls == 2  # 要約と下書きは別のキーなので、どちらも Gemini を呼ぶ

    # もう一度呼んでも、それぞれ自分の保存済み結果を返す（互いのキャッシュを使わない）
    summary_result_2 = asyncio.run(server.summarize_conversation("c1"))
    draft_result_2 = draft()

    assert gemini.calls == 2
    assert "保存済み" in summary_result_2
    assert "保存済み" in draft_result_2
    assert "まだ投稿していません" not in summary_result
    assert "まだ投稿していません" in draft_result


# --- start の既定値（未投稿の始まり） ---


def test_draft_without_posts_starts_from_the_beginning(gemini):
    result = draft()

    assert "index 0〜2" in result
    assert "投稿済みなので" not in result


def test_draft_default_start_skips_posted_messages(gemini):
    record_post(["m0"])

    result = draft()

    assert "index 1〜2" in result


def test_draft_default_start_adds_a_note_about_the_skip(gemini):
    record_post(["m0"])

    result = draft()

    assert result.startswith("index 0〜0 は投稿済みなので index 1 から下書きにした（全部使うなら start=0）")


def test_draft_explicit_start_overrides_the_default(gemini):
    record_post(["m0", "m1", "m2"])  # 全部投稿済みでも、start を指定すれば作れる

    result = draft(start=0)

    assert "index 0〜2" in result
    assert "投稿済みなので" not in result


def test_draft_fully_posted_without_start_is_error(gemini):
    record_post(["m0", "m1", "m2"])

    with pytest.raises(ValueError, match="投稿済み"):
        draft()
