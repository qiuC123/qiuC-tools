"""Tests for read-only local HTML and Markdown import."""

from pathlib import Path

import pytest

from wxcli.errors import NotFoundError, ValidationError
from wxcli.models import Provider
from wxcli.providers.local import LocalFileProvider


def test_html_is_converted_to_markdown_and_prefers_data_src(tmp_path: Path) -> None:
    source = tmp_path / "article.html"
    source.write_text(
        """<html><head><title>Fallback</title></head><body>
        <h1 id="activity-name">本地文章</h1>
        <div id="js_content"><p>第一段</p><img src="fallback.jpg" data-src="image.jpg"></div>
        </body></html>""",
        encoding="utf-8",
    )

    article = LocalFileProvider().get(source)

    assert article.title == "本地文章"
    assert article.content_markdown == "第一段\n\n![](image.jpg)"
    assert article.images == ["image.jpg"]
    assert article.provider is Provider.LOCAL


def test_markdown_keeps_content_and_discovers_images(tmp_path: Path) -> None:
    source = tmp_path / "article.md"
    source.write_text("# Markdown 标题\n\n正文\n\n![图片](image.png)", encoding="utf-8")

    article = LocalFileProvider().get(source)

    assert article.title == "Markdown 标题"
    assert article.content_markdown.startswith("# Markdown 标题")
    assert article.images == ["image.png"]


def test_missing_and_unsupported_local_files_have_expected_errors(tmp_path: Path) -> None:
    with pytest.raises(NotFoundError):
        LocalFileProvider().get(tmp_path / "missing.html")

    unsupported = tmp_path / "article.txt"
    unsupported.write_text("plain text", encoding="utf-8")
    with pytest.raises(ValidationError):
        LocalFileProvider().get(unsupported)
