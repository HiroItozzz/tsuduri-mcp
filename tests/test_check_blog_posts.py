"""check_blog_posts ツールのテスト（通信なし。BlogPoster は偽物に差し替える）"""

import asyncio
import json

import pytest

from tsuduri_mcp import blog, server
from tsuduri_mcp.sources import ClaudeExportSource
from tsuduri_mcp.store import ConversationStore, connect, default_db_path

from claude_export import raw_conversation, raw_message

MEMBER_URI = "https://blog.hatena.ne.jp/user/blog.example.com/atom/entry/1"


class FakePoster(blog.BlogPoster):
    service = "hatena"

    def __init__(self, article: blog.FetchedArticle | None):
        self.article = article
        self.requested_uris: list[str] = []

    async def post(self, article: blog.BlogArticle, *, draft: bool) -> blog.PostResult:
        raise NotImplementedError  # このテストファイルでは使わない

    async def get(self, member_uri: str) -> blog.FetchedArticle | None:
        self.requested_uris.append(member_uri)
        return self.article

    async def publish(self, member_uri: str, article: blog.BlogArticle, *, at) -> blog.PostResult:
        raise NotImplementedError  # このテストファイルでは使わない

    async def unpublish(self, member_uri: str, article: blog.BlogArticle) -> blog.PostResult:
        raise NotImplementedError  # このテストファイルでは使わない


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


def record_post(url: str = "https://blog.example.com/entry/1", member_uri: str | None = MEMBER_URI):
    conn = connect(default_db_path())
    try:
        ConversationStore(conn).record_post("c1", "hatena", url, "記事タイトル", ["m0", "m1"], member_uri)
    finally:
        conn.close()


def list_posts():
    conn = connect(default_db_path())
    try:
        return ConversationStore(conn).list_posts("c1")
    finally:
        conn.close()


def check(monkeypatch, fake_article, **kwargs) -> tuple[str, FakePoster]:
    fake = FakePoster(fake_article)
    monkeypatch.setattr(server, "make_poster", lambda service: fake)
    result = asyncio.run(server.check_blog_posts("c1", **kwargs))
    return result, fake


def test_no_posts_is_reported(monkeypatch):
    result, _ = check(monkeypatch, None)

    assert "投稿の記録はありません" in result


def test_shows_current_status_from_hatena(monkeypatch):
    record_post()

    result, fake = check(monkeypatch, fetched())

    assert fake.requested_uris == [MEMBER_URI]
    assert "記事タイトル" in result
    assert "下書き" in result
    assert "https://blog.example.com/entry/1" in result
    assert "https://blog.hatena.ne.jp/user/blog.example.com/edit?entry=1" in result
    assert "Python" in result
    assert f"本文 {len('# 見出し\n本文')} 文字" in result
    assert "index 0〜1" in result
    assert "2026-09-27T10:00:00+09:00" in result


def test_updates_url_when_it_changed_and_says_so(monkeypatch):
    record_post(url="https://blog.example.com/entry/OLD")

    result, _ = check(monkeypatch, fetched(url="https://blog.example.com/entry/NEW"))

    assert "更新しました" in result
    assert list_posts()[0].url == "https://blog.example.com/entry/NEW"


def test_url_note_is_absent_when_url_is_unchanged(monkeypatch):
    record_post(url="https://blog.example.com/entry/1")

    result, _ = check(monkeypatch, fetched(url="https://blog.example.com/entry/1"))

    assert "更新しました" not in result
    assert list_posts()[0].url == "https://blog.example.com/entry/1"


def test_deleted_article_is_reported_and_record_is_kept(monkeypatch):
    record_post()

    result, _ = check(monkeypatch, None)

    assert "削除されたようです" in result
    assert list_posts()[0].url == "https://blog.example.com/entry/1"  # 記録は残る


def test_missing_member_uri_is_reported(monkeypatch):
    record_post(member_uri=None)

    result, fake = check(monkeypatch, fetched())

    assert fake.requested_uris == []  # 確かめに行かない
    assert "メンバー URI がないので確かめられない" in result


def test_post_id_is_shown(monkeypatch):
    record_post()
    post_id = list_posts()[0].id

    result, _ = check(monkeypatch, fetched())

    assert f"post={post_id}" in result


def test_scheduled_article_is_shown_as_reserved(monkeypatch):
    record_post()

    result, _ = check(monkeypatch, fetched(is_draft=True, scheduled=True, updated="2026-12-31T09:00:00+09:00"))

    assert "予約（公開 2026-12-31T09:00:00+09:00）" in result
    assert "下書き" not in result


def test_content_is_shown_only_when_include_content_is_true(monkeypatch):
    record_post()

    without = check(monkeypatch, fetched(), include_content=False)[0]
    with_content = check(monkeypatch, fetched(), include_content=True)[0]

    assert "# 見出し" not in without
    assert "# 見出し" in with_content
