"""Contract tests for the strict read-only Official Account provider."""

from __future__ import annotations

import json
from collections.abc import Callable
from io import StringIO
from typing import Any, TypeVar

import httpx
import pytest

from wxcli.auth import AccessTokenInvalid
from wxcli.errors import ErrorCode, NotFoundError, ValidationError, WxcliError
from wxcli.models import Provider
from wxcli.output import Output
from wxcli.providers.official import OfficialAccountProvider

T = TypeVar("T")


class RetryTokens:
    """A fake that reproduces TokenManager's one-refresh/one-retry boundary."""

    def __init__(self) -> None:
        self.used: list[str] = []

    def with_token_retry(self, call: Callable[[str], T]) -> T:
        self.used.append("cached-token")
        try:
            return call("cached-token")
        except AccessTokenInvalid:
            self.used.append("refreshed-token")
            return call("refreshed-token")


def news(title: str, *, source_url: str = "https://mp.weixin.qq.com/s/source") -> dict[str, Any]:
    return {
        "title": title,
        "author": "示例作者",
        "content": f'<p>{title}正文</p><img src="fallback.jpg" data-src="{title}.jpg">',
        "url": source_url,
    }


def draft_item(media_id: str = "media_123") -> dict[str, Any]:
    return {
        "media_id": media_id,
        "content": {"news_item": [news("头条"), news("次条")]},
        "update_time": 1_760_000_000,
    }


def published_item(article_id: str = "article_123") -> dict[str, Any]:
    return {
        "article_id": article_id,
        "content": {"news_item": [news("已发布头条"), news("已发布次条")]},
        "update_time": 1_760_000_000,
    }


def provider_for(
    responder: Callable[[httpx.Request], httpx.Response],
) -> tuple[OfficialAccountProvider, RetryTokens, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responder(request)

    tokens = RetryTokens()
    client = httpx.Client(transport=httpx.MockTransport(record))
    return OfficialAccountProvider(client, tokens), tokens, requests


def request_body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


def test_draft_list_preserves_all_articles_and_zero_based_index() -> None:
    provider, _, requests = provider_for(
        lambda request: httpx.Response(
            200,
            json={"total_count": 1, "item_count": 1, "item": [draft_item()]},
        )
    )

    messages = provider.list_drafts(offset=0, count=10)

    assert len(messages) == 1
    assert messages[0].media_id == "media_123"
    assert [(article.index, article.title) for article in messages[0].articles] == [
        (0, "头条"),
        (1, "次条"),
    ]
    first = messages[0].articles[0]
    assert first.provider is Provider.OFFICIAL
    assert first.images == ["头条.jpg"]
    assert "![](头条.jpg)" in first.content_markdown
    assert requests[0].url.path == "/cgi-bin/draft/batchget"
    assert request_body(requests[0]) == {"offset": 0, "count": 10, "no_content": 0}


def test_official_message_list_is_one_json_document() -> None:
    provider, _, _ = provider_for(
        lambda request: httpx.Response(
            200,
            json={"total_count": 1, "item_count": 1, "item": [draft_item()]},
        )
    )
    stdout = StringIO()

    Output(json_mode=True, stdout=stdout, stderr=StringIO()).success(provider.list_drafts())

    payload = json.loads(stdout.getvalue())
    assert payload["ok"] is True
    assert [article["index"] for article in payload["data"][0]["articles"]] == [0, 1]


def test_draft_get_uses_only_strict_media_id() -> None:
    provider, _, requests = provider_for(
        lambda request: httpx.Response(
            200,
            json={"news_item": [news("草稿详情"), news("草稿次条")]},
        )
    )

    message = provider.get_draft("media:opaque/123")

    assert message.media_id == "media:opaque/123"
    assert [article.index for article in message.articles] == [0, 1]
    assert requests[0].url.path == "/cgi-bin/draft/get"
    assert request_body(requests[0]) == {"media_id": "media:opaque/123"}


def test_published_list_and_get_use_article_id_and_publish_time() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("batchget"):
            return httpx.Response(
                200,
                json={"total_count": 1, "item_count": 1, "item": [published_item()]},
            )
        return httpx.Response(
            200,
            json={"news_item": [news("发布详情")], "create_time": 1_760_000_000},
        )

    provider, _, requests = provider_for(respond)

    listed = provider.list_published()[0]
    detailed = provider.get_published("article:opaque/123")

    assert listed.article_id == "article_123"
    assert listed.articles[0].published_at is not None
    assert listed.articles[0].published_at.utcoffset().total_seconds() == 8 * 3600
    assert detailed.article_id == "article:opaque/123"
    assert requests[1].url.path == "/cgi-bin/freepublish/getarticle"
    assert request_body(requests[1]) == {"article_id": "article:opaque/123"}


@pytest.mark.parametrize("bad_id", ["", " leading", "trailing ", "line\nbreak", "x" * 513])
def test_identifiers_are_rejected_before_network(bad_id: str) -> None:
    provider, _, requests = provider_for(
        lambda request: pytest.fail("invalid IDs must not reach the network")
    )

    with pytest.raises(ValidationError):
        provider.get_draft(bad_id)
    with pytest.raises(ValidationError):
        provider.get_published(bad_id)
    assert requests == []


def test_pagination_is_rejected_before_network() -> None:
    provider, _, requests = provider_for(
        lambda request: pytest.fail("invalid pagination must not reach the network")
    )

    with pytest.raises(ValidationError):
        provider.list_drafts(offset=-1, count=1)
    with pytest.raises(ValidationError):
        provider.list_published(offset=0, count=21)
    assert requests == []


@pytest.mark.parametrize(
    "payload",
    [
        {"total_count": 1, "item_count": 1, "item": "not-an-array"},
        {"total_count": 1, "item_count": 1, "item": [{"media_id": "media_1"}]},
        {
            "total_count": 1,
            "item_count": 1,
            "item": [{"media_id": "media_1", "content": {"news_item": []}}],
        },
        {
            "total_count": 1,
            "item_count": 1,
            "item": [
                {"media_id": "media_1", "content": {"news_item": [{"content": "body"}]}}
            ],
        },
    ],
)
def test_malformed_official_data_is_never_silently_skipped(payload: dict[str, Any]) -> None:
    provider, _, _ = provider_for(lambda request: httpx.Response(200, json=payload))

    with pytest.raises(WxcliError) as raised:
        provider.list_drafts()
    assert raised.value.code is ErrorCode.PARSING_ERROR


def test_empty_list_without_item_array_is_valid() -> None:
    provider, _, _ = provider_for(
        lambda request: httpx.Response(200, json={"total_count": 0, "item_count": 0})
    )
    assert provider.list_drafts() == []


@pytest.mark.parametrize(
    ("method", "errcode"),
    [("draft", 40007), ("published", 53600)],
)
def test_missing_detail_maps_to_not_found(method: str, errcode: int) -> None:
    provider, _, _ = provider_for(
        lambda request: httpx.Response(200, json={"errcode": errcode, "errmsg": "missing"})
    )
    with pytest.raises(NotFoundError):
        if method == "draft":
            provider.get_draft("media_404")
        else:
            provider.get_published("article_404")


def test_permission_error_is_mapped_without_copying_server_message() -> None:
    provider, _, _ = provider_for(
        lambda request: httpx.Response(
            200,
            json={"errcode": 48001, "errmsg": "server text must not be copied"},
        )
    )

    with pytest.raises(WxcliError) as raised:
        provider.list_published()
    assert raised.value.code is ErrorCode.AUTHENTICATION_ERROR
    assert raised.value.details == {"errcode": 48001}


def test_invalid_token_is_refreshed_and_retried_once() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params["access_token"] == "cached-token":
            return httpx.Response(200, json={"errcode": 40014, "errmsg": "invalid token"})
        return httpx.Response(200, json={"total_count": 0, "item_count": 0})

    provider, tokens, _ = provider_for(respond)

    assert provider.list_drafts() == []
    assert tokens.used == ["cached-token", "refreshed-token"]
