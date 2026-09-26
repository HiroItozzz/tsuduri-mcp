"""はてなブログ投稿のテスト（通信なし）"""

import asyncio
import re
import xml.etree.ElementTree as ET
from urllib.parse import unquote

import httpx2
import pytest

from tsuduri_mcp.blog import BlogArticle, HatenaPoster

ENTRY_URL = "https://blog.hatena.ne.jp/user/blog.example.com/atom/entry"
NS = {"atom": "http://www.w3.org/2005/Atom", "app": "http://www.w3.org/2007/app"}

RESPONSE_XML = """<?xml version="1.0" encoding="utf-8"?>
<entry xmlns="http://www.w3.org/2005/Atom" xmlns:app="http://www.w3.org/2007/app">
  <link rel="edit" href="https://blog.hatena.ne.jp/user/blog.example.com/atom/entry/2500000000"/>
  <link rel="alternate" type="text/html" href="https://blog.example.com/entry/2025/11/20/100000"/>
  <title>記事タイトル</title>
  <updated>2025-11-20T10:00:00+09:00</updated>
  <content type="text/x-markdown">本文</content>
  <category term="Python" />
  <category term="自動投稿" />
  <app:control>
    <app:draft>yes</app:draft>
  </app:control>
</entry>
"""


def make_poster(client: httpx2.AsyncClient | None = None, consumer_key: str = "c_key") -> HatenaPoster:
    return HatenaPoster(
        entry_url=ENTRY_URL,
        consumer_key=consumer_key,
        consumer_secret="c_secret",
        access_token="r_key",
        access_token_secret="r_secret",
        client=client,
    )


def article(categories: list[str] | None = None) -> BlogArticle:
    return BlogArticle(
        title="タイトル", content="# 見出し\n本文", categories=["Python", "学習"] if categories is None else categories
    )


def parse_entry(xml_str: str) -> ET.Element:
    return ET.fromstring(xml_str)


# --- リクエストXML ---


def test_request_xml_contains_article():
    root = parse_entry(make_poster()._build_entry(article(), draft=True))

    title = root.find("atom:title", NS)
    assert title is not None
    assert title.text == "タイトル"
    content = root.find("atom:content", NS)
    assert content is not None
    assert content.text == "# 見出し\n本文"
    assert content.get("type") == "text/x-markdown"


def test_request_xml_contains_categories():
    root = parse_entry(make_poster()._build_entry(article(), draft=True))

    terms = [c.get("term") for c in root.findall("atom:category", NS)]
    assert terms == ["Python", "学習"]


@pytest.mark.parametrize(("draft", "expected"), [(True, "yes"), (False, "no")])
def test_request_xml_draft_flag(draft, expected):
    root = parse_entry(make_poster()._build_entry(article(), draft=draft))

    draft_elem = root.find("app:control/app:draft", NS)
    assert draft_elem is not None
    assert draft_elem.text == expected


def test_updated_is_now_in_jst():
    root = parse_entry(make_poster()._build_entry(article(), draft=True))

    updated = root.find("atom:updated", NS)
    assert updated is not None and updated.text is not None
    assert updated.text.endswith("+09:00")


# --- レスポンス解析 ---


def test_parse_response():
    result = make_poster()._parse_response(RESPONSE_XML)

    assert result.title == "記事タイトル"
    assert result.url == "https://blog.example.com/entry/2025/11/20/100000"
    assert result.edit_url == "https://blog.hatena.ne.jp/user/blog.example.com/edit?entry=2500000000"
    assert result.is_draft is True


# --- 投稿（MockTransport） ---


def test_post_sends_signed_xml_to_entry_url():
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(201, text=RESPONSE_XML)

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            return await make_poster(client=client).post(article(), draft=True)

    result = asyncio.run(run())

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == ENTRY_URL
    assert request.headers["Authorization"].startswith("OAuth ")
    assert 'oauth_consumer_key="c_key"' in request.headers["Authorization"]
    title = parse_entry(request.content.decode()).find("atom:title", NS)
    assert title is not None
    assert title.text == "タイトル"
    assert result.url == "https://blog.example.com/entry/2025/11/20/100000"


def test_post_keeps_angle_bracket_credentials_as_is():
    """はてなの認証情報は `<>` がついたままの値で動くので、加工せずそのまま署名に使う。

    OAuth 署名の Authorization ヘッダーはどんな値でも毎回パーセントエンコードされるので、それを戻して確かめる。
    """
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(201, text=RESPONSE_XML)

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            poster = make_poster(client=client, consumer_key="<consumer_key>")
            await poster.post(article(), draft=True)

    asyncio.run(run())

    match = re.search(r'oauth_consumer_key="([^"]+)"', requests[0].headers["Authorization"])
    assert match is not None
    assert unquote(match.group(1)) == "<consumer_key>"


def test_post_raises_runtime_error_when_not_201():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(400, text="Bad Request: something is wrong")

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            await make_poster(client=client).post(article(), draft=True)

    with pytest.raises(RuntimeError, match="400"):
        asyncio.run(run())


# --- from_env ---


def test_from_env_reads_all_variables(monkeypatch):
    monkeypatch.setenv("HATENA_ENTRY_URL", ENTRY_URL)
    monkeypatch.setenv("HATENA_CONSUMER_KEY", "<c_key>")
    monkeypatch.setenv("HATENA_CONSUMER_SECRET", "c_secret")
    monkeypatch.setenv("HATENA_ACCESS_TOKEN", "<r_key>")
    monkeypatch.setenv("HATENA_ACCESS_TOKEN_SECRET", "r_secret")

    poster = HatenaPoster.from_env()

    assert poster.entry_url == ENTRY_URL
    assert poster.consumer_key == "<c_key>"  # <> がついたままでも加工しない
    assert poster.access_token == "<r_key>"


def test_from_env_lists_missing_variables(monkeypatch):
    for name in (
        "HATENA_ENTRY_URL",
        "HATENA_CONSUMER_KEY",
        "HATENA_CONSUMER_SECRET",
        "HATENA_ACCESS_TOKEN",
        "HATENA_ACCESS_TOKEN_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HATENA_ENTRY_URL", ENTRY_URL)

    with pytest.raises(RuntimeError) as excinfo:
        HatenaPoster.from_env()

    message = str(excinfo.value)
    assert "HATENA_CONSUMER_KEY" in message
    assert "HATENA_ENTRY_URL" not in message  # 足りている変数は出さない
