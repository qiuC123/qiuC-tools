"""Strict, read-only Official Account draft and published-message provider."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Protocol, TypeVar

import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify  # type: ignore[import-untyped]
from pydantic import HttpUrl, TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from wxcli.auth import raise_for_official_error
from wxcli.errors import ErrorCode, NotFoundError, ValidationError, WxcliError
from wxcli.models import Article, DraftMessage, Provider, PublishedMessage

_DRAFT_BATCHGET = "https://api.weixin.qq.com/cgi-bin/draft/batchget"
_DRAFT_GET = "https://api.weixin.qq.com/cgi-bin/draft/get"
_PUBLISHED_BATCHGET = "https://api.weixin.qq.com/cgi-bin/freepublish/batchget"
_PUBLISHED_GET = "https://api.weixin.qq.com/cgi-bin/freepublish/getarticle"
_CHINA_TIME = timezone(timedelta(hours=8))
_HTTP_URL = TypeAdapter(HttpUrl)

T = TypeVar("T")


class TokenProvider(Protocol):
    """The controlled one-refresh retry operation required by this provider."""

    def with_token_retry(self, call: Callable[[str], T]) -> T: ...


class OfficialAccountProvider:
    """Read drafts and publications without modifying Official Account data."""

    def __init__(self, client: httpx.Client, tokens: TokenProvider) -> None:
        self._client = client
        self._tokens = tokens

    def list_drafts(self, *, offset: int = 0, count: int = 20) -> list[DraftMessage]:
        self._validate_page(offset, count)
        payload = self._post(
            _DRAFT_BATCHGET,
            {"offset": offset, "count": count, "no_content": 0},
        )
        return [self._draft_from_list_item(item) for item in self._list_items(payload)]

    def get_draft(self, media_id: str) -> DraftMessage:
        media_id = self._validate_id(media_id, "media_id")
        payload = self._post(_DRAFT_GET, {"media_id": media_id}, not_found_codes={40007})
        news_items = self._required_news_items(payload)
        return DraftMessage(
            media_id=media_id,
            articles=self._articles(news_items, published_at=None),
        )

    def list_published(self, *, offset: int = 0, count: int = 20) -> list[PublishedMessage]:
        self._validate_page(offset, count)
        payload = self._post(
            _PUBLISHED_BATCHGET,
            {"offset": offset, "count": count, "no_content": 0},
        )
        return [self._published_from_list_item(item) for item in self._list_items(payload)]

    def get_published(self, article_id: str) -> PublishedMessage:
        article_id = self._validate_id(article_id, "article_id")
        payload = self._post(
            _PUBLISHED_GET,
            {"article_id": article_id},
            not_found_codes={53600},
        )
        published_at = self._timestamp(payload.get("create_time") or payload.get("update_time"))
        return PublishedMessage(
            article_id=article_id,
            articles=self._articles(self._required_news_items(payload), published_at),
        )

    def _post(
        self,
        url: str,
        body: Mapping[str, object],
        *,
        not_found_codes: set[int] | None = None,
    ) -> dict[str, Any]:
        return self._tokens.with_token_retry(
            lambda token: self._request(url, body, token, not_found_codes or set())
        )

    def _request(
        self,
        url: str,
        body: Mapping[str, object],
        token: str,
        not_found_codes: set[int],
    ) -> dict[str, Any]:
        try:
            response = self._client.post(
                url,
                params={"access_token": token},
                json=dict(body),
            )
        except httpx.HTTPError as error:
            raise WxcliError(
                ErrorCode.NETWORK_ERROR,
                "The Official Account API could not be reached.",
            ) from error
        try:
            payload = response.json()
        except ValueError as error:
            raise WxcliError(
                ErrorCode.NETWORK_ERROR,
                "The Official Account API returned invalid JSON.",
            ) from error
        if not isinstance(payload, dict):
            raise WxcliError(
                ErrorCode.PARSING_ERROR,
                "The Official Account API returned an unexpected object.",
            )
        errcode = payload.get("errcode")
        if isinstance(errcode, int) and errcode in not_found_codes:
            raise NotFoundError("The requested Official Account message was not found.")
        raise_for_official_error(payload)
        if response.status_code != 200:
            raise WxcliError(
                ErrorCode.NETWORK_ERROR,
                "The Official Account API returned an HTTP error.",
            )
        return payload

    @classmethod
    def _draft_from_list_item(cls, value: object) -> DraftMessage:
        item = cls._required_mapping(value, "draft item")
        media_id = cls._response_id(item.get("media_id"), "media_id")
        content = cls._required_mapping(item.get("content"), "draft content")
        return DraftMessage(
            media_id=media_id,
            articles=cls._articles(cls._required_news_items(content), published_at=None),
        )

    @classmethod
    def _published_from_list_item(cls, value: object) -> PublishedMessage:
        item = cls._required_mapping(value, "published item")
        article_id = cls._response_id(item.get("article_id"), "article_id")
        content = cls._required_mapping(item.get("content"), "published content")
        return PublishedMessage(
            article_id=article_id,
            articles=cls._articles(
                cls._required_news_items(content),
                cls._timestamp(item.get("update_time")),
            ),
        )

    @classmethod
    def _articles(
        cls,
        news_items: list[object],
        published_at: datetime | None,
    ) -> list[Article]:
        return [
            cls._article(cls._required_mapping(value, "news item"), index, published_at)
            for index, value in enumerate(news_items)
        ]

    @classmethod
    def _article(
        cls,
        news: Mapping[str, Any],
        index: int,
        published_at: datetime | None,
    ) -> Article:
        title = cls._required_text(news.get("title"), "article title")
        html = news.get("content")
        if not isinstance(html, str):
            raise cls._parse_error("Article content must be a string.")
        soup = BeautifulSoup(html, "lxml")
        images: list[str] = []
        for image in soup.select("img"):
            value = image.get("data-src") or image.get("src")
            if value:
                images.append(str(value))
            if data_src := image.get("data-src"):
                image["src"] = str(data_src)
        return Article(
            index=index,
            title=title,
            content_markdown=markdownify(str(soup), heading_style="ATX").strip(),
            source_url=cls._source_url(news),
            author=cls._optional_text(news.get("author")),
            published_at=published_at,
            images=images,
            provider=Provider.OFFICIAL,
        )

    @classmethod
    def _source_url(cls, news: Mapping[str, Any]) -> HttpUrl | None:
        value = news.get("url") or news.get("content_source_url")
        if value in (None, ""):
            return None
        if not isinstance(value, str):
            raise cls._parse_error("Article source URL must be a string.")
        try:
            return _HTTP_URL.validate_python(value)
        except PydanticValidationError as error:
            raise cls._parse_error("Article source URL is invalid.") from error

    @classmethod
    def _list_items(cls, payload: Mapping[str, Any]) -> list[object]:
        item_count = payload.get("item_count")
        if not isinstance(item_count, int) or item_count < 0:
            raise cls._parse_error("Official Account item_count must be a non-negative integer.")
        value = payload.get("item")
        if value is None and item_count == 0:
            return []
        if not isinstance(value, list):
            raise cls._parse_error("Official Account list items must be an array.")
        if len(value) != item_count:
            raise cls._parse_error("Official Account item_count does not match the item array.")
        return value

    @classmethod
    def _required_news_items(cls, value: Mapping[str, Any]) -> list[object]:
        news_items = value.get("news_item")
        if not isinstance(news_items, list) or not news_items:
            raise cls._parse_error("A message must contain at least one news item.")
        return news_items

    @classmethod
    def _required_mapping(cls, value: object, name: str) -> Mapping[str, Any]:
        if not isinstance(value, dict):
            raise cls._parse_error(f"The {name} must be an object.")
        return value

    @classmethod
    def _response_id(cls, value: object, name: str) -> str:
        if not isinstance(value, str) or not cls._is_valid_id(value):
            raise cls._parse_error(f"The API returned an invalid {name}.")
        return value

    @classmethod
    def _validate_id(cls, value: str, name: str) -> str:
        if not cls._is_valid_id(value):
            raise ValidationError(
                f"The {name} must be a non-empty opaque ID without surrounding whitespace or control characters."
            )
        return value

    @staticmethod
    def _is_valid_id(value: str) -> bool:
        return (
            1 <= len(value) <= 512
            and value == value.strip()
            and all(character.isprintable() for character in value)
        )

    @staticmethod
    def _validate_page(offset: int, count: int) -> None:
        if offset < 0 or not 1 <= count <= 20:
            raise ValidationError("offset must be at least 0 and count must be between 1 and 20.")

    @staticmethod
    def _required_text(value: object, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise OfficialAccountProvider._parse_error(f"The {name} must not be empty.")
        return value.strip()

    @staticmethod
    def _optional_text(value: object) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _timestamp(value: object) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, int | float) or value < 0:
            raise OfficialAccountProvider._parse_error("The message timestamp is invalid.")
        return datetime.fromtimestamp(value, tz=UTC).astimezone(_CHINA_TIME)

    @staticmethod
    def _parse_error(message: str) -> WxcliError:
        return WxcliError(ErrorCode.PARSING_ERROR, message)
