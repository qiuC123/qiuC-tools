"""Public, read-only HTTP retrieval of WeChat article pages."""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from wxcli.cache import ArticleCache
from wxcli.errors import ErrorCode, NotFoundError, VerificationRequiredError, WxcliError
from wxcli.models import Article, Provider
from wxcli.public_article import PublicArticleDocument, PublicArticleParser
from wxcli.public_url import validate_public_url

_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 5


class PageKind(StrEnum):
    ARTICLE = "article"
    VERIFICATION = "verification"
    NOT_FOUND = "not_found"
    ERROR = "error"


class WeChatPageClassifier:
    """Classify a 200 page before attempting article extraction."""

    def classify(self, html: str) -> PageKind:
        soup = BeautifulSoup(html, "lxml")
        has_article_shell = bool(
            soup.select_one("#activity-name") and soup.select_one("#js_content")
        )
        if has_article_shell:
            for content in soup.select("#js_content"):
                content.decompose()
        text = soup.get_text(" ", strip=True)
        if any(marker in text for marker in ("环境异常", "访问过于频繁", "请完成安全验证", "验证码")):
            return PageKind.VERIFICATION
        if any(marker in text for marker in ("内容已被发布者删除", "页面不存在", "内容已被删除")):
            return PageKind.NOT_FOUND
        if has_article_shell:
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
        return self.get_document(normalized_url, no_cache=no_cache).article

    def get_document(self, url: str, *, no_cache: bool = False) -> PublicArticleDocument:
        """Fetch a fresh page document; Article-only cache entries never fabricate evidence."""
        normalized_url = validate_public_url(url)
        response = self._request(normalized_url)
        kind = self.classifier.classify(response.text)
        if kind is PageKind.VERIFICATION:
            raise VerificationRequiredError()
        if kind is PageKind.NOT_FOUND:
            raise NotFoundError("The public article was not found.")
        if kind is PageKind.ERROR:
            raise WxcliError(ErrorCode.PARSING_ERROR, "The WeChat page is not a readable article.")

        document = PublicArticleParser.parse(response.text, normalized_url, Provider.HTTP)
        if self.cache and not no_cache:
            self.cache.put(normalized_url, document.article)
        return document

    def _request(self, url: str) -> httpx.Response:
        current = url
        try:
            for _ in range(_MAX_REDIRECTS + 1):
                response = self.client.get(current, follow_redirects=False)
                if response.status_code not in _REDIRECT_CODES:
                    break
                location = response.headers.get("location")
                if not location:
                    raise WxcliError(
                        ErrorCode.NETWORK_ERROR,
                        "The public article returned an invalid redirect.",
                    )
                current = self._safe_redirect(current, location)
            else:
                raise WxcliError(
                    ErrorCode.NETWORK_ERROR,
                    "The public article returned too many redirects.",
                )
        except httpx.HTTPError as error:
            raise WxcliError(ErrorCode.NETWORK_ERROR, "The public article could not be fetched.") from error
        if response.status_code == 404:
            raise NotFoundError("The public article was not found.")
        if response.status_code != 200:
            raise WxcliError(ErrorCode.NETWORK_ERROR, "The public article returned an HTTP error.")
        return response

    @staticmethod
    def _safe_redirect(current: str, location: str) -> str:
        target = urljoin(current, location)
        try:
            parsed = urlsplit(target)
            port = parsed.port
        except ValueError as error:
            raise WxcliError(ErrorCode.NETWORK_ERROR, "The public article redirect is invalid.") from error
        if (
            parsed.scheme.casefold() != "https"
            or (parsed.hostname or "").casefold() != "mp.weixin.qq.com"
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
        ):
            raise WxcliError(
                ErrorCode.NETWORK_ERROR,
                "The public article refused an external redirect.",
            )
        return target

    @staticmethod
    def _parse(html: str, url: str) -> Article:
        return PublicArticleParser.parse(html, url, Provider.HTTP).article
