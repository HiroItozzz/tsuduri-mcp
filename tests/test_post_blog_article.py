"""post_blog_article ツールのテスト（通信なし。BlogPoster は偽物に差し替える）"""

import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tsuduri_mcp import blog, server
from tsuduri_mcp.sources import ClaudeExportSource
from tsuduri_mcp.store import ConversationStore, connect, default_db_path

from claude_export import raw_conversation, raw_message


class FakePoster(blog.BlogPoster):
    service = "hatena"

    def __init__(self, error: Exception | None = None):
        self.calls: list[tuple[blog.BlogArticle, bool]] = []
        self._error = error

    async def post(self, article: blog.BlogArticle, *, draft: bool) -> blog.PostResult:
        self.calls.append((article, draft))
        if self._error is not None:
            raise self._error
        return blog.PostResult(
            title=article.title,
            url="https://blog.example.com/entry/1",
            edit_url="https://blog.hatena.ne.jp/user/blog.example.com/edit?entry=1",
            is_draft=draft,
        )


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    # 本線は m0, m2（m1 は m0 から分かれた古い枝）
    conversations = [
        raw_conversation(
            "c1",
            [
                raw_message("m0", text="テストの質問です"),
                raw_message("m1", sender="assistant", text="古い枝の答え", parent="m0"),
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


@pytest.fixture
def fake_poster(monkeypatch):
    fake = FakePoster()
    monkeypatch.setattr(server, "make_poster", lambda service: fake)
    return fake


def list_posts(conversation_uuid: str = "c1"):
    conn = connect(default_db_path())
    try:
        return ConversationStore(conn).list_posts(conversation_uuid)
    finally:
        conn.close()


def post(**kwargs):
    kwargs.setdefault("conversation_uuid", "c1")
    kwargs.setdefault("title", "タイトル")
    kwargs.setdefault("content", "本文")
    return asyncio.run(server.post_blog_article(**kwargs))


def test_posts_and_records_the_range(fake_poster):
    result = post(categories=["Python"])

    assert len(fake_poster.calls) == 1
    article, draft = fake_poster.calls[0]
    assert article.title == "タイトル"
    assert article.content == "本文"
    assert article.categories == ["Python"]
    assert draft is True  # 既定は下書き

    posts = list_posts()
    assert len(posts) == 1
    assert posts[0].url == "https://blog.example.com/entry/1"
    # 本線は m0, m2 の2件
    assert posts[0].min_position == 0
    assert posts[0].max_position == 2

    assert "下書き" in result
    assert "https://blog.example.com/entry/1" in result
    assert "https://blog.hatena.ne.jp/user/blog.example.com/edit?entry=1" in result
    assert "index 0〜2（この枝の 2 件）" in result


def test_publish_true_posts_without_draft(fake_poster):
    post(publish=True)

    _, draft = fake_poster.calls[0]
    assert draft is False


def test_range_error_prevents_posting(fake_poster):
    # index=1 は本線にない（本線は m0, m2。m1 は古い枝）
    with pytest.raises(ToolError):
        post(start=1)

    assert fake_poster.calls == []
    assert list_posts() == []


def test_failed_post_is_not_recorded(fake_poster):
    fake_poster._error = RuntimeError("投稿に失敗しました")

    with pytest.raises(ToolError, match="投稿に失敗しました"):
        post()

    assert list_posts() == []


def test_unsupported_service_is_rejected():
    with pytest.raises(ToolError, match="まだ対応していません"):
        post(service="qiita")
