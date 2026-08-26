"""Public, read-only HTTP retrieval of WeChat article pages."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum
from typing import cast

import httpx
from bs4 import BeautifulSoup, Tag
from markdownify import markdownify  # type: ignore[import-untyped]
from pydantic import HttpUrl

from wxcli.cache import ArticleCache
from wxcli.errors import ErrorCode, NotFoundError, VerificationRequiredError, WxcliError
from wxcli.models import Article, Provider
from wxcli.public_url import validate_public_url

_CHINA_TIME = timezone(timedelta(hours=8))


class PageKind(StrEnum):
    ARTICLE = "article"
    VERIFICATION = "verification"
    NOT_FOUND = "not_found"
    ERROR = "error"


class WeChatPageClassifier:
    """Classify a 200 page before attempting article extraction."""

    def classify(self, html: str) -> PageKind:
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text(" ", strip=True)
        if any(marker in text for marker in ("环境异常", "访问过于频繁", "请完成安全验证", "验证码")):
            return PageKind.VERIFICATION
        if any(marker in text for marker in ("内容已被发布者删除", "页面不存在", "内容已被删除")):
            return PageKind.NOT_FOUND
        if soup.select_one("#activity-name") and soup.select_one("#js_content"):
            return PageKind.ARTICLE
        return PageKind.ERROR


class PublicHttpProvider:
    """Fetch and parse a supported public article URL without writing to WeChat."""

    def __init__(self, client: httpx.Client, cache: ArticleCache | None = None) -> None:
        self.client = client
        self.cache = cache
        self.classifier = WeChatPageClassifier()

    def get(self, url: str, *, no_cache: bool = False) -> Article:
        normalized_url = validate_public_url(url)
        if self.cache and not no_cache:
            if article := self.cache.get(normalized_url):
                return article
        try:
            response = self.client.get(normalized_url, follow_redirects=True)
        except httpx.HTTPError as error:
            raise WxcliError(ErrorCode.NETWORK_ERROR, "The public article could not be fetched.") from error
        if response.status_code == 404:
            raise NotFoundError("The public article was not found.")
        if response.status_code != 200:
            raise WxcliError(ErrorCode.NETWORK_ERROR, "The public article returned an HTTP error.")

        kind = self.classifier.classify(response.text)
        if kind is PageKind.VERIFICATION:
            raise VerificationRequiredError()
        if kind is PageKind.NOT_FOUND:
            raise NotFoundError("The public article was not found.")
        if kind is PageKind.ERROR:
            raise WxcliError(ErrorCode.PARSING_ERROR, "The WeChat page is not a readable article.")

        article = self._parse(response.text, normalized_url)
        if self.cache and not no_cache:
            self.cache.put(normalized_url, article)
        return article

    @staticmethod
    def _parse(html: str, url: str) -> Article:
        soup = BeautifulSoup(html, "lxml")
        content = soup.select_one("#js_content")
        title = PublicHttpProvider._text(soup.select_one("#activity-name")) or PublicHttpProvider._og(soup, "og:title")
        if not content or not title:
            raise WxcliError(ErrorCode.PARSING_ERROR, "The article page is missing required content.")
        images = PublicHttpProvider._images(content)
        for image in content.select("img"):
            if data_src := image.get("data-src"):
                image["src"] = str(data_src)
        return Article(
            title=title,
            content_markdown=markdownify(str(content), heading_style="ATX").strip(),
            source_url=cast(HttpUrl, url),
            author=PublicHttpProvider._text(soup.select_one("#js_name")) or None,
            published_at=PublicHttpProvider._published_at(html),
            images=images,
            provider=Provider.HTTP,
        )

    @staticmethod
    def _text(node: Tag | None) -> str:
        return node.get_text(" ", strip=True) if node else ""

    @staticmethod
    def _og(soup: BeautifulSoup, property_name: str) -> str:
        tag = soup.find("meta", attrs={"property": property_name})
        return str(tag.get("content", "")).strip() if tag else ""

    @staticmethod
    def _images(content: Tag) -> list[str]:
        return [str(value) for image in content.select("img") if (value := image.get("data-src") or image.get("src"))]

    @staticmethod
    def _published_at(html: str) -> datetime | None:
        match = re.search(r"(?:createTime|\bct)\s*[=:]\s*['\"]?(\d{10})", html)
        if not match:
            return None
        return datetime.fromtimestamp(int(match.group(1)), tz=UTC).astimezone(_CHINA_TIME)
