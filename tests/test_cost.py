"""料金の計算・記録のテスト（通信なし。FunctionModel は Gemini の代わり）。

genai-prices はモデル名だけでも料金を引けるので、FunctionModel に既知のモデル名を渡すだけで
（本物の Google には繋がずに）料金が計算できるかどうかを確かめられる。
"""

import asyncio
import json

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from tsuduri_mcp import server
from tsuduri_mcp.sources import ClaudeExportSource
from tsuduri_mcp.store import ConversationStore, connect

from claude_export import raw_conversation, raw_message


def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[TextPart("テストの要約")])


@pytest.fixture
def conversation(tmp_path, monkeypatch):
    msgs = [
        raw_message("m0", text="最初のテストの質問"),
        raw_message("m1", sender="assistant", text="最初のテストの答え", parent="m0"),
    ]
    src = tmp_path / "conversations.json"
    src.write_text(json.dumps([raw_conversation("c1", msgs)], ensure_ascii=False), encoding="utf-8")
    path = tmp_path / "tsuduri.db"
    conn = connect(path)
    ConversationStore(conn).import_conversations(ClaudeExportSource(src).load())
    conn.close()
    monkeypatch.setenv("TSUDURI_DB", str(path))


def summarize(**kwargs):
    return asyncio.run(server.summarize_conversation("c1", **kwargs))


def llm_calls():
    conn = connect(server.default_db_path())
    calls = ConversationStore(conn).list_llm_calls("c1")
    conn.close()
    return calls


def test_known_model_gets_a_positive_cost(conversation, monkeypatch):
    # SUMMARY_MODEL（既定は gemini-3-flash-preview）は genai-prices が知っているモデル名なので、
    # FunctionModel で実際の通信をしていなくても料金が計算できる
    monkeypatch.setattr(server, "make_model", lambda model_name: FunctionModel(respond, model_name=model_name))

    result = summarize()

    assert "約 $" in result
    calls = llm_calls()
    assert len(calls) == 1
    assert calls[0].cost_usd is not None
    assert calls[0].cost_usd > 0


def test_unknown_model_name_does_not_crash_and_cost_is_unknown(conversation, monkeypatch):
    monkeypatch.setattr(server, "SUMMARY_MODEL", "no-such-model-xyz")
    monkeypatch.setattr(server, "make_model", lambda model_name: FunctionModel(respond, model_name=model_name))

    result = summarize()

    assert "料金は不明" in result
    calls = llm_calls()
    assert len(calls) == 1
    assert calls[0].cost_usd is None


def test_cached_summary_does_not_record_another_llm_call(conversation, monkeypatch):
    monkeypatch.setattr(server, "make_model", lambda model_name: FunctionModel(respond, model_name=model_name))
    summarize()

    result = summarize()

    assert "保存済み（今回の料金なし）" in result
    assert len(llm_calls()) == 1  # 2回目は Gemini を呼んでいないので増えない
