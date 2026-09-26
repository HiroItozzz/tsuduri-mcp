"""ブログサービスへの投稿。サービスごとに BlogPoster を実装し、interface を揃える（sources.py と同じ考え方）。"""

import os
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx2
from authlib.integrations.httpx_client import OAuth1Auth

JST = timezone(timedelta(hours=9))


@dataclass
class BlogArticle:
    title: str
    content: str
    categories: list[str] = field(default_factory=list)


@dataclass
class PostResult:
    title: str
    url: str
    edit_url: str
    is_draft: bool


class BlogPoster(ABC):
    service: str

    @abstractmethod
    async def post(self, article: BlogArticle, *, draft: bool) -> PostResult: ...


def _safe_find(root: ET.Element, key: str, ns: dict[str, str], default: str = "") -> str:
    """ヘルパー関数: 見つからない・本文なしの場合は空文字にする"""
    elem = root.find(key, ns)
    return elem.text if elem is not None and elem.text is not None else default


def _safe_find_attr(root: ET.Element, key: str, attr: str, ns: dict[str, str], default: str = "") -> str:
    """属性取得用ヘルパー関数"""
    elem = root.find(key, ns)
    return elem.get(attr, default) if elem is not None else default


class HatenaPoster(BlogPoster):
    """はてなブログの AtomPub API への投稿。"""

    service = "hatena"

    NS = {"atom": "http://www.w3.org/2005/Atom", "app": "http://www.w3.org/2007/app"}

    def __init__(
        self,
        entry_url: str,
        consumer_key: str,
        consumer_secret: str,
        access_token: str,
        access_token_secret: str,
        client: httpx2.AsyncClient | None = None,
    ):
        # はてなの認証情報は `<...>` がついたままの値で動くので、加工したり形式チェックで弾いたりしない
        self.entry_url = entry_url
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret
        self.access_token = access_token
        self.access_token_secret = access_token_secret
        self._client = client

    @classmethod
    def from_env(cls) -> "HatenaPoster":
        entry_url = os.environ.get("HATENA_ENTRY_URL")
        consumer_key = os.environ.get("HATENA_CONSUMER_KEY")
        consumer_secret = os.environ.get("HATENA_CONSUMER_SECRET")
        access_token = os.environ.get("HATENA_ACCESS_TOKEN")
        access_token_secret = os.environ.get("HATENA_ACCESS_TOKEN_SECRET")
        values = {
            "HATENA_ENTRY_URL": entry_url,
            "HATENA_CONSUMER_KEY": consumer_key,
            "HATENA_CONSUMER_SECRET": consumer_secret,
            "HATENA_ACCESS_TOKEN": access_token,
            "HATENA_ACCESS_TOKEN_SECRET": access_token_secret,
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise RuntimeError(f"環境変数が足りません: {', '.join(missing)}")
        assert entry_url and consumer_key and consumer_secret and access_token and access_token_secret
        return cls(entry_url, consumer_key, consumer_secret, access_token, access_token_secret)

    async def post(self, article: BlogArticle, *, draft: bool) -> PostResult:
        xml_entry = self._build_entry(article, draft=draft)
        auth = OAuth1Auth(
            client_id=self.consumer_key,
            client_secret=self.consumer_secret,
            token=self.access_token,
            token_secret=self.access_token_secret,
            force_include_body=True,
        )
        headers = {"Content-Type": "application/xml; charset=utf-8"}
        if self._client is not None:
            response = await self._client.post(self.entry_url, auth=auth, content=xml_entry, headers=headers)
        else:
            async with httpx2.AsyncClient() as client:
                response = await client.post(self.entry_url, auth=auth, content=xml_entry, headers=headers)
        if response.status_code != 201:
            raise RuntimeError(
                f"はてなブログへの投稿に失敗しました（status={response.status_code}）: {response.text[:200]}"
            )
        return self._parse_response(response.text)

    def _build_entry(self, article: BlogArticle, *, draft: bool) -> str:
        """はてなブログ投稿リクエストの Atom XML を組み立てる。"""
        root = ET.Element(
            "entry",
            attrib={
                "xmlns": "http://www.w3.org/2005/Atom",
                "xmlns:app": "http://www.w3.org/2007/app",
            },
        )
        title_elem = ET.SubElement(root, "title")
        updated_elem = ET.SubElement(root, "updated")
        content_elem = ET.SubElement(root, "content", attrib={"type": "text/x-markdown"})
        control = ET.SubElement(root, "app:control")
        draft_elem = ET.SubElement(control, "app:draft")
        for category in article.categories:
            ET.SubElement(root, "category", attrib={"term": category})

        title_elem.text = article.title
        updated_elem.text = datetime.now(JST).isoformat()
        content_elem.text = article.content
        draft_elem.text = "yes" if draft else "no"

        return ET.tostring(root, encoding="unicode")

    def _parse_response(self, text: str) -> PostResult:
        """投稿結果を取得"""
        root = ET.fromstring(text)
        edit_api_url = _safe_find_attr(root, "atom:link[@rel='edit']", "href", self.NS)
        edit_url = edit_api_url.replace("atom/entry/", "edit?entry=")
        return PostResult(
            title=_safe_find(root, "atom:title", self.NS),
            url=_safe_find_attr(root, "atom:link[@rel='alternate']", "href", self.NS),
            edit_url=edit_url,
            is_draft=_safe_find(root, "app:control/app:draft", self.NS) == "yes",
        )
