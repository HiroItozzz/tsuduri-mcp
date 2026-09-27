"""publish_blog_post / unpublish_blog_post ツールのテスト（通信なし。BlogPoster は偽物に差し替える）"""

import asyncio
import json
from datetime import datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tsuduri_mcp import blog, server
from tsuduri_mcp.sources import ClaudeExportSource
from tsuduri_mcp.store import ConversationStore, connect, default_db_path

from claude_export import raw_conversation, raw_message

MEMBER_URI = "https://blog.hatena.ne.jp/user/blog.example.com/atom/entry/1"


class FakePoster(blog.BlogPoster):
    service = "hatena"

    def __init__(self, article: blog.FetchedArticle | None):
        self.article = article
        self.publish_calls: list[tuple[str, blog.BlogArticle, datetime | None]] = []
        self.unpublish_calls: list[tuple[str, blog.BlogArticle]] = []

    async def post(self, article: blog.BlogArticle, *, draft: bool) -> blog.PostResult:
        raise NotImplementedError  # このテストファイルでは使わない

    async def get(self, member_uri: str) -> blog.FetchedArticle | None:
        return self.article

    async def publish(self, member_uri: str, article: blog.BlogArticle, *, at: datetime | None) -> blog.PostResult:
        self.publish_calls.append((member_uri, article, at))
        return blog.PostResult(
            title=article.title,
            url="https://blog.example.com/entry/PUBLISHED",
            edit_url="https://blog.hatena.ne.jp/user/blog.example.com/edit?entry=1",
            member_uri=member_uri,
            is_draft=at is not None,
        )

    async def unpublish(self, member_uri: str, article: blog.BlogArticle) -> blog.PostResult:
        self.unpublish_calls.append((member_uri, article))
        return blog.PostResult(
            title=article.title,
            url="https://blog.example.com/entry/DRAFT",
            edit_url="https://blog.hatena.ne.jp/user/blog.example.com/edit?entry=1",
            member_uri=member_uri,
            is_draft=True,
        )


def fetched(**overrides) -> blog.FetchedArticle:
    values = {
        "title": "記事タイトル",
        "content": "# 見出し\n本文",
        "categories": ["Python"],
        "is_draft": True,
        "url": "https://blog.example.com/entry/1",
        "edit_url": "https://blog.hatena.ne.jp/user/blog.example.com/edit?entry=1",
        "updated": "2026-09-27T10:00:00+09:00",
        "edited": "2026-09-27T10:00:00+09:00",
        "scheduled": False,
    }
    values.update(overrides)
    return blog.FetchedArticle(**values)


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    conversations = [
        raw_conversation(
            "c1",
            [
                raw_message("m0", text="テストの質問です"),
                raw_message("m1", sender="assistant", text="テストの答え", parent="m0"),
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


def record_post(url: str = "https://blog.example.com/entry/1", member_uri: str | None = MEMBER_URI) -> int:
    conn = connect(default_db_path())
    try:
        return ConversationStore(conn).record_post("c1", "hatena", url, "記事タイトル", ["m0", "m1"], member_uri)
    finally:
        conn.close()


def list_posts():
    conn = connect(default_db_path())
    try:
        return ConversationStore(conn).list_posts("c1")
    finally:
        conn.close()


def check_fake(monkeypatch, fake_article) -> FakePoster:
    fake = FakePoster(fake_article)
    monkeypatch.setattr(server, "make_poster", lambda service: fake)
    return fake


def publish(post_id: int, **kwargs):
    kwargs.setdefault("conversation_uuid", "c1")
    return asyncio.run(server.publish_blog_post(post_id=post_id, **kwargs))


def unpublish(post_id: int, **kwargs):
    kwargs.setdefault("conversation_uuid", "c1")
    return asyncio.run(server.unpublish_blog_post(post_id=post_id, **kwargs))


# --- publish_blog_post ---


def test_publish_without_confirm_does_not_put(monkeypatch):
    post_id = record_post()
    fake = check_fake(monkeypatch, fetched())

    result = publish(post_id)

    assert fake.publish_calls == []
    assert "confirm=true" in result


def test_publish_default_delay_is_one_minute(monkeypatch):
    post_id = record_post()
    fake = check_fake(monkeypatch, fetched())

    before = datetime.now(blog.JST).replace(microsecond=0)  # 予約の時刻は秒まで
    publish(post_id, confirm=True)
    after = datetime.now(blog.JST)

    assert len(fake.publish_calls) == 1
    _, _, at = fake.publish_calls[0]
    assert at is not None
    assert before + timedelta(minutes=1) <= at <= after + timedelta(minutes=1)


def test_publish_delay_zero_publishes_immediately(monkeypatch):
    post_id = record_post()
    fake = check_fake(monkeypatch, fetched())

    publish(post_id, delay_minutes=0, confirm=True)

    assert len(fake.publish_calls) == 1
    _, _, at = fake.publish_calls[0]
    assert at is None


def test_publish_uses_hatena_content_not_local_draft(monkeypatch):
    post_id = record_post()
    fake = check_fake(monkeypatch, fetched(title="はてなの今のタイトル", content="はてなの今の本文", categories=["Q"]))

    publish(post_id, confirm=True)

    _, article, _ = fake.publish_calls[0]
    assert article.title == "はてなの今のタイトル"
    assert article.content == "はてなの今の本文"
    assert article.categories == ["Q"]


def test_publish_skips_put_when_already_published(monkeypatch):
    post_id = record_post()
    fake = check_fake(monkeypatch, fetched(is_draft=False, scheduled=False))

    result = publish(post_id, confirm=True)

    assert fake.publish_calls == []
    assert "すでに公開されています" in result


def test_publish_skips_put_when_already_scheduled(monkeypatch):
    post_id = record_post()
    fake = check_fake(monkeypatch, fetched(is_draft=True, scheduled=True, updated="2026-12-31T09:00:00+09:00"))

    result = publish(post_id, confirm=True)

    assert fake.publish_calls == []
    assert "すでに予約されています" in result
    assert "2026-12-31T09:00:00+09:00" in result


def test_publish_missing_member_uri_is_an_error(monkeypatch):
    post_id = record_post(member_uri=None)
    fake = check_fake(monkeypatch, fetched())

    with pytest.raises(ToolError, match="メンバー URI"):
        publish(post_id, confirm=True)

    assert fake.publish_calls == []


def test_publish_article_not_found_in_hatena_is_an_error(monkeypatch):
    post_id = record_post()
    check_fake(monkeypatch, None)

    with pytest.raises(ToolError, match="見つかりません"):
        publish(post_id, confirm=True)


def test_publish_unknown_post_id_is_an_error(monkeypatch):
    record_post()
    check_fake(monkeypatch, fetched())

    with pytest.raises(ToolError):
        publish(post_id=99999, confirm=True)


def test_publish_updates_recorded_url(monkeypatch):
    post_id = record_post()
    check_fake(monkeypatch, fetched())

    publish(post_id, confirm=True)

    assert list_posts()[0].url == "https://blog.example.com/entry/PUBLISHED"


# --- unpublish_blog_post ---


def test_unpublish_without_confirm_does_not_put(monkeypatch):
    post_id = record_post()
    fake = check_fake(monkeypatch, fetched(is_draft=False, scheduled=False))

    result = unpublish(post_id)

    assert fake.unpublish_calls == []
    assert "confirm=true" in result


def test_unpublish_from_scheduled(monkeypatch):
    post_id = record_post()
    fake = check_fake(monkeypatch, fetched(is_draft=True, scheduled=True, updated="2026-12-31T09:00:00+09:00"))

    result = unpublish(post_id, confirm=True)

    assert len(fake.unpublish_calls) == 1
    assert "下書きに戻しました" in result


def test_unpublish_from_published(monkeypatch):
    post_id = record_post()
    fake = check_fake(monkeypatch, fetched(is_draft=False, scheduled=False))

    unpublish(post_id, confirm=True)

    assert len(fake.unpublish_calls) == 1


def test_unpublish_skips_put_when_already_draft(monkeypatch):
    post_id = record_post()
    fake = check_fake(monkeypatch, fetched(is_draft=True, scheduled=False))

    result = unpublish(post_id, confirm=True)

    assert fake.unpublish_calls == []
    assert "すでに下書きです" in result


def test_unpublish_missing_member_uri_is_an_error(monkeypatch):
    post_id = record_post(member_uri=None)
    fake = check_fake(monkeypatch, fetched())

    with pytest.raises(ToolError, match="メンバー URI"):
        unpublish(post_id, confirm=True)

    assert fake.unpublish_calls == []


def test_unpublish_article_not_found_in_hatena_is_an_error(monkeypatch):
    post_id = record_post()
    check_fake(monkeypatch, None)

    with pytest.raises(ToolError, match="見つかりません"):
        unpublish(post_id, confirm=True)


def test_unpublish_updates_recorded_url(monkeypatch):
    post_id = record_post()
    check_fake(monkeypatch, fetched(is_draft=False, scheduled=False))

    unpublish(post_id, confirm=True)

    assert list_posts()[0].url == "https://blog.example.com/entry/DRAFT"
