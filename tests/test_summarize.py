import asyncio
import json

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from tsuduri_mcp import llm, server
from tsuduri_mcp.sources import ClaudeExportSource
from tsuduri_mcp.store import ConversationStore, connect

from claude_export import raw_conversation, raw_message


class FakeGemini:
    """Gemini の代わり。受け取った会話ログを記録して、決まった要約を返す。"""

    def __init__(self):
        self.prompts: list[str] = []

    def model(self, model_name: str) -> FunctionModel:
        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            self.prompts += [
                part.content
                for m in messages
                if isinstance(m, ModelRequest)
                for part in m.parts
                if isinstance(part, UserPromptPart) and isinstance(part.content, str)
            ]
            return ModelResponse(parts=[TextPart(f"テストの要約 {len(self.prompts)} 回目")])

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


def summarize(**kwargs):
    return asyncio.run(server.summarize_conversation("c1", **kwargs))


def test_summary_is_made_from_the_line(gemini):
    result = summarize()

    assert "テストの要約 1 回目" in result
    assert "index 0〜2（3 件）" in result
    assert "index=0" in gemini.prompts[0]
    assert "最初のテストの答え" in gemini.prompts[0]


def test_second_call_uses_saved_summary(gemini):
    summarize()

    result = summarize()

    assert len(gemini.prompts) == 1
    assert "テストの要約 1 回目" in result
    assert "保存済み" in result


def test_refresh_calls_gemini_again(gemini):
    summarize()

    result = summarize(refresh=True)

    assert len(gemini.prompts) == 2
    assert "テストの要約 2 回目" in result


def test_start_limits_the_range_and_is_saved_separately(gemini):
    summarize()

    result = summarize(start=2)

    assert "index 2〜2（1 件）" in result
    assert "最初のテストの質問" not in gemini.prompts[1]
    assert len(gemini.prompts) == 2


def test_too_long_conversation_is_error(gemini, monkeypatch):
    monkeypatch.setattr(server, "MAX_INPUT_CHARS", 10)

    with pytest.raises(ValueError):
        summarize()
    assert gemini.prompts == []


def test_transcript_version_bump_invalidates_cache(gemini, monkeypatch):
    # render_transcript の形が変わったとみなして版を上げると、保存済みの要約を使い回さない
    summarize()

    monkeypatch.setattr(server, "TRANSCRIPT_VERSION", server.TRANSCRIPT_VERSION + 1)
    result = summarize()

    assert len(gemini.prompts) == 2
    assert "テストの要約 2 回目" in result


@pytest.fixture
def gemini_branchy(tmp_path, monkeypatch):
    #  m0 ─ m1 ─ m2old            （古い枝）
    #            └ m2new          （こちらが新しい）
    msgs = [
        raw_message("m0", text="最初のテストの質問", created_at="2026-01-01T00:00:00.000000Z"),
        raw_message(
            "m1", sender="assistant", text="最初のテストの答え", parent="m0", created_at="2026-01-01T00:01:00.000000Z"
        ),
        raw_message("m2old", text="古い枝の質問", parent="m1", created_at="2026-01-01T00:02:00.000000Z"),
        raw_message("m2new", text="新しい枝の質問", parent="m1", created_at="2026-01-01T00:03:00.000000Z"),
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


def test_different_branches_are_summarized_and_cached_separately(gemini_branchy):
    main_result = summarize()  # 既定は新しい枝（m2new）を通る本線
    other_result = summarize(through_index=2)  # index=2 は古い枝（m2old）

    assert len(gemini_branchy.prompts) == 2  # 枝ごとに別のキーなので、どちらも Gemini を呼ぶ

    # もう一度呼んでも、それぞれ自分の保存済み結果を返す
    again_main = summarize()
    again_other = summarize(through_index=2)

    assert len(gemini_branchy.prompts) == 2
    assert "保存済み" in again_main
    assert "保存済み" in again_other
    assert main_result != other_result


def test_missing_api_key_is_clear_error(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        llm.gemini()


def test_summary_prompt_exists():
    assert "index" in llm.load_prompt("summary")
