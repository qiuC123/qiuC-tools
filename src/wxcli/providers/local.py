"""Read HTML and Markdown files without changing the local filesystem."""

from __future__ import annotations

import re
from pathlib import Path

from bs4 import BeautifulSoup, Tag
from markdownify import markdownify  # type: ignore[import-untyped]

from wxcli.errors import ErrorCode, NotFoundError, ValidationError, WxcliError
from wxcli.models import Article, Provider

_HTML_EXTENSIONS = frozenset({".html", ".htm"})
_MARKDOWN_EXTENSIONS = frozenset({".md", ".markdown"})
_SUPPORTED_EXTENSIONS = _HTML_EXTENSIONS | _MARKDOWN_EXTENSIONS


class LocalFileProvider:
    """Convert a supported local HTML or Markdown file into an Article."""

    def get(self, path: Path) -> Article:
        """Load a local file and return its read-only Article representation."""
        if not path.exists() or not path.is_file():
            raise NotFoundError("The local file does not exist.")
        if path.suffix.lower() not in _SUPPORTED_EXTENSIONS:
            raise ValidationError("Only HTML and Markdown local files are supported.")
        try:
            content = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as error:
            raise WxcliError(
                ErrorCode.PARSING_ERROR,
                "The local file must be UTF-8 encoded.",
            ) from error
        except OSError as error:
            raise WxcliError(
                ErrorCode.LOCAL_CONFIGURATION_ERROR,
                "The local file could not be read.",
            ) from error

        if path.suffix.lower() in _HTML_EXTENSIONS:
            return self._from_html(content, path)
        return self._from_markdown(content, path)

    def _from_html(self, content: str, path: Path) -> Article:
        soup = BeautifulSoup(content, "lxml")
        node = self._content_node(soup)
        title = self._first_text(soup.select_one("#activity-name"))
        if not title:
            title = self._first_text(soup.select_one("title"))
        if not title:
            title = self._first_text(soup.select_one("h1")) or path.stem
        images = self._image_urls(node)
        for image in node.select("img"):
            if data_src := image.get("data-src"):
                image["src"] = str(data_src)
        return Article(
            title=title,
            content_markdown=markdownify(str(node), heading_style="ATX").strip(),
            images=images,
            provider=Provider.LOCAL,
        )

    def _from_markdown(self, content: str, path: Path) -> Article:
        title_match = re.search(r"^#\s+(.+?)\s*$", content, flags=re.MULTILINE)
        title = title_match.group(1) if title_match else path.stem
        image_urls = re.findall(r"!\[[^]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)", content)
        return Article(
            title=title,
            content_markdown=content.strip(),
            images=image_urls,
            provider=Provider.LOCAL,
        )

    @staticmethod
    def _content_node(soup: BeautifulSoup) -> Tag | BeautifulSoup:
        return (
            soup.select_one("#js_content")
            or soup.select_one("article")
            or soup.select_one("main")
            or soup.body
            or soup
        )

    @staticmethod
    def _first_text(node: Tag | None) -> str:
        return node.get_text(" ", strip=True) if node else ""

    @staticmethod
    def _image_urls(node: Tag | BeautifulSoup) -> list[str]:
        urls: list[str] = []
        for image in node.select("img"):
            if value := image.get("data-src") or image.get("src"):
                urls.append(str(value))
        return urls
