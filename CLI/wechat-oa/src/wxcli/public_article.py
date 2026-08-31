"""Internal parsing of a public WeChat page into an Article and source observations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag
from markdownify import markdownify  # type: ignore[import-untyped]
from pydantic import HttpUrl

from wxcli.errors import ErrorCode, WxcliError
from wxcli.models import Article, Provider

_CHINA_TIME = timezone(timedelta(hours=8))
_BIZ_SCRIPT = re.compile(r"(?:\bvar\s+)?(?:__biz|biz)\s*[=:]\s*['\"]([^'\"]+)['\"]")
_PUBLISHED_SCRIPT = re.compile(
    r"(?:createTime|\bct)\s*[=:]\s*['\"]?(\d{10})(?!\d)"
)


@dataclass(frozen=True, slots=True)
class ObservedExternalLink:
    """One supported link observed directly in the article body."""

    index: int
    source_location: str
    raw_value: str
    normalized_value: str
    kind: str
    text: str | None


@dataclass(frozen=True, slots=True)
class PublicArticleDocument:
    """A parsed Article plus observations that are intentionally not Article fields."""

    article: Article
    html: str
    normalized_url: str
    account_display_name: str | None
    account_biz_id: str | None
    external_links: tuple[ObservedExternalLink, ...]


class PublicArticleParser:
    """Parse a classified WeChat article page without performing network requests."""

    @classmethod
    def parse(cls, html: str, url: str, provider: Provider) -> PublicArticleDocument:
        soup = BeautifulSoup(html, "lxml")
        content = soup.select_one("#js_content")
        title = cls._text(soup.select_one("#activity-name")) or cls._og(soup, "og:title")
        if not content or not title:
            raise WxcliError(ErrorCode.PARSING_ERROR, "The article page is missing required content.")

        account_name = cls._text(soup.select_one("#js_name")) or None
        images = cls._images(content)
        links = cls._external_links(content)
        for image in content.select("img"):
            if data_src := image.get("data-src"):
                image["src"] = str(data_src)
        content_markdown = markdownify(str(content), heading_style="ATX").strip()
        if not content_markdown and not images:
            raise WxcliError(
                ErrorCode.PARSING_ERROR,
                "The article page contains no extractable content.",
            )

        article = Article(
            title=title,
            content_markdown=content_markdown,
            source_url=HttpUrl(url),
            author=account_name,
            published_at=cls._published_at(soup),
            images=images,
            provider=provider,
        )
        return PublicArticleDocument(
            article=article,
            html=html,
            normalized_url=url,
            account_display_name=account_name,
            account_biz_id=cls._biz_id(soup, url),
            external_links=links,
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
        return [
            str(value)
            for image in content.select("img")
            if (value := image.get("data-src") or image.get("src"))
        ]

    @classmethod
    def _published_at(cls, soup: BeautifulSoup) -> datetime | None:
        for script in cls._trusted_script_texts(soup):
            if match := _PUBLISHED_SCRIPT.search(script):
                return datetime.fromtimestamp(int(match.group(1)), tz=UTC).astimezone(
                    _CHINA_TIME
                )
        return None

    @classmethod
    def _biz_id(cls, soup: BeautifulSoup, url: str) -> str | None:
        query_value = parse_qs(urlsplit(url).query).get("__biz", [])
        if len(query_value) == 1 and query_value[0]:
            return query_value[0]

        for selector in (
            "#js_name[href]",
            "a[href*='/mp/profile_ext'][href*='__biz=']",
        ):
            for node in soup.select(selector):
                if node.find_parent(id="js_content") is not None:
                    continue
                href = node.get("href")
                if not href:
                    continue
                values = parse_qs(urlsplit(str(href)).query).get("__biz", [])
                if len(values) == 1 and values[0]:
                    return values[0]

        for script in cls._trusted_script_texts(soup):
            if match := _BIZ_SCRIPT.search(script):
                return match.group(1) or None
        return None

    @staticmethod
    def _trusted_script_texts(soup: BeautifulSoup) -> list[str]:
        """Return page scripts only; article-body text and embedded scripts are untrusted."""
        return [
            str(script.string or script.get_text())
            for script in soup.find_all("script")
            if script.find_parent(id="js_content") is None
        ]

    @classmethod
    def _external_links(cls, content: Tag) -> tuple[ObservedExternalLink, ...]:
        values: list[ObservedExternalLink] = []
        for node in content.select("a[href]"):
            raw = str(node.get("href", "")).strip()
            normalized = cls._normalize_link(raw)
            if normalized is None:
                continue
            scheme = urlsplit(normalized).scheme.casefold()
            if scheme in {"http", "https"}:
                kind = (
                    "wechat"
                    if (urlsplit(normalized).hostname or "").casefold() == "mp.weixin.qq.com"
                    else "external_http"
                )
            elif scheme == "mailto":
                kind = "email"
            else:
                kind = "phone"
            text = cls._text(node) or None
            values.append(
                ObservedExternalLink(
                    index=len(values),
                    source_location=f"article_body:a[{len(values)}]",
                    raw_value=raw,
                    normalized_value=normalized,
                    kind=kind,
                    text=text,
                )
            )
        return tuple(values)

    @staticmethod
    def _normalize_link(value: str) -> str | None:
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            return None
        scheme = parsed.scheme.casefold()
        if scheme in {"mailto", "tel"}:
            return value if parsed.path else None
        if scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        netloc = parsed.hostname.casefold()
        if port is not None:
            netloc = f"{netloc}:{port}"
        return urlunsplit((scheme, netloc, parsed.path, parsed.query, ""))
