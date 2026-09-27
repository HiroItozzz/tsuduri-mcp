import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError
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

    with pytest.raises(ToolError, match="投稿済み"):
        draft()


# --- 次の一手・件数の表示（この枝の N 件） ---


def test_draft_tells_how_to_record_the_post(gemini):
    result = draft()

    assert "post_blog_article(start=0, end=2) だけでよい" in result


def test_draft_next_step_includes_through_index_when_given(gemini):
    result = draft(through_index=2)

    assert "post_blog_article(start=0, end=2, through_index=2) だけでよい" in result


def test_draft_shows_the_branch_count_not_the_whole_range(gemini):
    result = draft()

    assert "index 0〜2（この枝の 3 件）の下書き" in result


# --- 材料が薄いときの警告 ---


def test_draft_warns_when_material_is_thin(gemini):
    result = draft()

    assert "材料が少ない" in result


def test_draft_no_warning_when_material_is_enough(gemini, monkeypatch):
    monkeypatch.setattr(server, "MIN_MATERIAL_CHARS", 10)  # このフィクスチャの本文でも 10 文字は超える

    result = draft()

    assert "材料が少ない" not in result


def test_blog_prompt_warns_against_padding():
    from tsuduri_mcp import llm

    assert "足さない" in llm.load_prompt("blog")


# --- この会話への投稿の記録・枝分かれの知らせ ---


def test_draft_lists_previous_posts_in_this_conversation(gemini):
    record_post(["m0"], url="https://example.com/first", title="前の記事")

    result = draft(start=0)

    assert "この会話には投稿の記録があります:" in result
    assert "「前の記事」 hatena index 0〜0（この枝） post=1 https://example.com/first" in result


def test_draft_no_posts_note_without_any_post(gemini):
    result = draft()

    assert "投稿の記録があります" not in result


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


def test_draft_mentions_the_branch_count_when_forked(gemini_branchy):
    result = draft()

    assert "この会話は枝が 2 本あります。いま読んでいる枝: 本線。別の枝は through_index で選べます" in result
    # 本線は m0, m1, m2new の3件。seq=2（m2old）は別の枝なので抜ける
    assert "index 0〜3（この枝の 3 件）の下書き" in result


def test_draft_marks_a_post_recorded_on_a_different_branch(gemini_branchy):
    record_post(["m2old"])  # 古い枝（本線には入っていない）に投稿を記録

    result = draft()

    assert "index 2〜2（別の枝）" in result
