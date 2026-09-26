"""log.py と、ツール呼び出し・generate_cached が書くログのテスト（本文やキーワードが漏れないことを確かめる）。"""

import asyncio
import json
import logging

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from tsuduri_mcp import log, server
from tsuduri_mcp.sources import ClaudeExportSource
from tsuduri_mcp.store import ConversationStore, connect

from claude_export import raw_conversation, raw_message

SECRET_BODY = "とうふとわかめのみそしるがすき"
SECRET_KEYWORD = "ないしょのことば"


def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[TextPart("テストの要約")])


@pytest.fixture
def log_file(tmp_path, monkeypatch):
    path = tmp_path / "tsuduri.log"
    monkeypatch.setenv("TSUDURI_LOG", str(path))
    log.setup_logging()
    yield path
    # モジュール単位のロガーなので、他のテストに影響しないよう付けたハンドラを外しておく
    for handler in list(log.logger.handlers):
        log.logger.removeHandler(handler)
        handler.close()
    log.logger.setLevel(logging.NOTSET)


@pytest.fixture
def conversation(tmp_path, monkeypatch):
    msgs = [
        raw_message("m0", text=SECRET_BODY),
        raw_message("m1", sender="assistant", text="了解です", parent="m0"),
    ]
    src = tmp_path / "conversations.json"
    src.write_text(json.dumps([raw_conversation("c1", msgs)], ensure_ascii=False), encoding="utf-8")
    path = tmp_path / "tsuduri.db"
    conn = connect(path)
    ConversationStore(conn).import_conversations(ClaudeExportSource(src).load())
    conn.close()
    monkeypatch.setenv("TSUDURI_DB", str(path))


def test_setup_logging_writes_to_the_given_path(log_file):
    assert log_file.exists()


def test_tool_log_does_not_contain_message_body_or_search_keyword(log_file, conversation, monkeypatch):
    monkeypatch.setattr(server, "make_model", lambda model_name: FunctionModel(respond, model_name=model_name))

    asyncio.run(server.summarize_conversation("c1"))
    server.search_messages([SECRET_KEYWORD])

    content = log_file.read_text(encoding="utf-8")
    assert SECRET_BODY not in content
    assert SECRET_KEYWORD not in content
    # 会話の本文やキーワードは書かないが、ツール名や成否はわかるようにする
    assert "summarize_conversation" in content
    assert "search_messages" in content
    assert "成功" in content
