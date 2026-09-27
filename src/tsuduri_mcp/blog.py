"""ブログサービスへの投稿。サービスごとに BlogPoster を実装し、interface を揃える（sources.py と同じ考え方）。"""

import os
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx2
from authlib.integrations.httpx_client import OAuth1Auth

JST = timezone(timedelta(hours=9))

REQUEST_TIMEOUT_SECONDS = 30.0

# 通信に失敗しても、はてな側に届いているかどうかはこちらからはわからない
UNKNOWN_RESULT_HINT = "投稿されたかどうかわからないので、はてなの下書き一覧を確かめてから再実行してください"


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
    member_uri: str  # 記事の目印になる、変わらない URI（応答の edit リンクの href そのもの）
    is_draft: bool


@dataclass
class FetchedArticle:
    """はてなから読み出した、記事の今の状態。"""

    title: str
    content: str
    categories: list[str]
    is_draft: bool
    url: str  # 今の URL。下書きのあいだは編集するたびに変わる
    edit_url: str
    updated: str
    edited: str
    scheduled: bool  # 予約中（下書きのまま、指定の時刻に自動で公開される）かどうか


class BlogPoster(ABC):
    service: str

    @abstractmethod
    async def post(self, article: BlogArticle, *, draft: bool) -> PostResult: ...

    @abstractmethod
    async def get(self, member_uri: str) -> FetchedArticle | None:
        """member_uri の記事の今の状態を読み出す。見つからなければ None。"""
        ...

    @abstractmethod
    async def publish(self, member_uri: str, article: BlogArticle, *, at: datetime | None) -> PostResult:
        """member_uri の記事を公開する。at を渡すとその時刻に予約し、None ならすぐ公開する。

        予約の有無はサービスごとに違うので、インターフェースは「いつ公開するか」だけを受け取る。
        """
        ...

    @abstractmethod
    async def unpublish(self, member_uri: str, article: BlogArticle) -> PostResult:
        """member_uri の記事を下書きに戻す（予約中なら予約も取り消す）。"""
        ...


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

    NS = {
        "atom": "http://www.w3.org/2005/Atom",
        "app": "http://www.w3.org/2007/app",
        "hatenablog": "http://www.hatena.ne.jp/info/xmlns#hatenablog",
    }

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
        xml_entry = self._build_entry(article, draft=draft, updated=datetime.now(JST))
        auth = OAuth1Auth(
            client_id=self.consumer_key,
            client_secret=self.consumer_secret,
            token=self.access_token,
            token_secret=self.access_token_secret,
            force_include_body=True,
        )
        headers = {"Content-Type": "application/xml; charset=utf-8"}
        try:
            if self._client is not None:
                response = await self._client.post(self.entry_url, auth=auth, content=xml_entry, headers=headers)
            else:
                async with httpx2.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
                    response = await client.post(self.entry_url, auth=auth, content=xml_entry, headers=headers)
        except httpx2.HTTPError as e:
            raise RuntimeError(
                f"はてなブログとの通信に失敗しました（{type(e).__name__}）。{UNKNOWN_RESULT_HINT}"
            ) from e
        if response.status_code != 201:
            raise RuntimeError(
                f"はてなブログへの投稿に失敗しました（status={response.status_code}）: {response.text[:200]}"
            )
        try:
            return self._parse_response(response.text)
        except ET.ParseError as e:
            raise RuntimeError(
                f"はてなブログの応答を解釈できませんでした（{type(e).__name__}）。{UNKNOWN_RESULT_HINT}"
            ) from e

    def _build_entry(
        self, article: BlogArticle, *, draft: bool, updated: datetime | None, scheduled: bool | None = None
    ) -> str:
        """はてなブログ投稿リクエストの Atom XML を組み立てる。

        updated は None なら要素ごと省く（下書きに戻すときは、予約や公開の時刻を変えないため）。
        scheduled は None なら hatenablog:scheduled 要素を送らない（新規投稿のとき）。
        yes/no を明示すると、予約する・予約を取り消す。
        """
        root = ET.Element(
            "entry",
            attrib={
                "xmlns": "http://www.w3.org/2005/Atom",
                "xmlns:app": "http://www.w3.org/2007/app",
                "xmlns:hatenablog": "http://www.hatena.ne.jp/info/xmlns#hatenablog",
            },
        )
        title_elem = ET.SubElement(root, "title")
        updated_elem = ET.SubElement(root, "updated") if updated is not None else None
        content_elem = ET.SubElement(root, "content", attrib={"type": "text/x-markdown"})
        control = ET.SubElement(root, "app:control")
        draft_elem = ET.SubElement(control, "app:draft")
        scheduled_elem = ET.SubElement(control, "hatenablog:scheduled") if scheduled is not None else None
        for category in article.categories:
            ET.SubElement(root, "category", attrib={"term": category})

        title_elem.text = article.title
        if updated_elem is not None:
            assert updated is not None
            updated_elem.text = updated.isoformat()
        content_elem.text = article.content
        draft_elem.text = "yes" if draft else "no"
        if scheduled_elem is not None:
            scheduled_elem.text = "yes" if scheduled else "no"

        return ET.tostring(root, encoding="unicode")

    def _parse_response(self, text: str) -> PostResult:
        """投稿結果を取得"""
        root = ET.fromstring(text)
        member_uri = _safe_find_attr(root, "atom:link[@rel='edit']", "href", self.NS)
        edit_url = member_uri.replace("atom/entry/", "edit?entry=")
        return PostResult(
            title=_safe_find(root, "atom:title", self.NS),
            url=_safe_find_attr(root, "atom:link[@rel='alternate']", "href", self.NS),
            edit_url=edit_url,
            member_uri=member_uri,
            is_draft=_safe_find(root, "app:control/app:draft", self.NS) == "yes",
        )

    async def get(self, member_uri: str) -> FetchedArticle | None:
        auth = OAuth1Auth(
            client_id=self.consumer_key,
            client_secret=self.consumer_secret,
            token=self.access_token,
            token_secret=self.access_token_secret,
            force_include_body=True,
        )
        try:
            if self._client is not None:
                response = await self._client.get(member_uri, auth=auth)
            else:
                async with httpx2.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
                    response = await client.get(member_uri, auth=auth)
        except httpx2.HTTPError as e:
            raise RuntimeError(f"はてなブログとの通信に失敗しました（{type(e).__name__}）") from e
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise RuntimeError(
                f"はてなブログの記事を読み出せませんでした（status={response.status_code}）: {response.text[:200]}"
            )
        try:
            return self._parse_fetched(response.text)
        except ET.ParseError as e:
            raise RuntimeError(f"はてなブログの応答を解釈できませんでした（{type(e).__name__}）") from e

    def _parse_fetched(self, text: str) -> FetchedArticle:
        """記事の今の状態を取得"""
        root = ET.fromstring(text)
        member_uri = _safe_find_attr(root, "atom:link[@rel='edit']", "href", self.NS)
        edit_url = member_uri.replace("atom/entry/", "edit?entry=")
        return FetchedArticle(
            title=_safe_find(root, "atom:title", self.NS),
            content=_safe_find(root, "atom:content", self.NS),
            categories=[c.get("term", "") for c in root.findall("atom:category", self.NS)],
            is_draft=_safe_find(root, "app:control/app:draft", self.NS) == "yes",
            url=_safe_find_attr(root, "atom:link[@rel='alternate']", "href", self.NS),
            edit_url=edit_url,
            updated=_safe_find(root, "atom:updated", self.NS),
            edited=_safe_find(root, "app:edited", self.NS),
            scheduled=_safe_find(root, "app:control/hatenablog:scheduled", self.NS) == "yes",
        )

    async def _put(
        self, member_uri: str, article: BlogArticle, *, draft: bool, updated: datetime | None, scheduled: bool | None
    ) -> PostResult:
        """公開・予約・下書きに戻すで共通の PUT（記事を丸ごと置き換える）。成功は 200。"""
        xml_entry = self._build_entry(article, draft=draft, updated=updated, scheduled=scheduled)
        auth = OAuth1Auth(
            client_id=self.consumer_key,
            client_secret=self.consumer_secret,
            token=self.access_token,
            token_secret=self.access_token_secret,
            force_include_body=True,
        )
        headers = {"Content-Type": "application/xml; charset=utf-8"}
        try:
            if self._client is not None:
                response = await self._client.put(member_uri, auth=auth, content=xml_entry, headers=headers)
            else:
                async with httpx2.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
                    response = await client.put(member_uri, auth=auth, content=xml_entry, headers=headers)
        except httpx2.HTTPError as e:
            raise RuntimeError(
                f"はてなブログとの通信に失敗しました（{type(e).__name__}）。{UNKNOWN_RESULT_HINT}"
            ) from e
        if response.status_code != 200:
            raise RuntimeError(
                f"はてなブログの更新に失敗しました（status={response.status_code}）: {response.text[:200]}"
            )
        try:
            return self._parse_response(response.text)
        except ET.ParseError as e:
            raise RuntimeError(
                f"はてなブログの応答を解釈できませんでした（{type(e).__name__}）。{UNKNOWN_RESULT_HINT}"
            ) from e

    async def publish(self, member_uri: str, article: BlogArticle, *, at: datetime | None) -> PostResult:
        if at is None:
            return await self._put(member_uri, article, draft=False, updated=datetime.now(JST), scheduled=False)
        return await self._put(member_uri, article, draft=True, updated=at, scheduled=True)

    async def unpublish(self, member_uri: str, article: BlogArticle) -> PostResult:
        return await self._put(member_uri, article, draft=True, updated=None, scheduled=False)
