"""Mocked tests for public HTTP page classification, parsing, and cache behavior."""

from pathlib import Path

import httpx
import pytest

from wxcli.cache import ArticleCache
from wxcli.errors import NotFoundError, VerificationRequiredError
from wxcli.providers.http import PublicHttpProvider


def make_provider(tmp_path: Path, html: str, status_code: int = 200) -> PublicHttpProvider:
    transport = httpx.MockTransport(lambda request: httpx.Response(status_code, text=html))
    return PublicHttpProvider(httpx.Client(transport=transport), ArticleCache(tmp_path / "cache"))


def test_article_page_is_parsed_and_cached(tmp_path: Path) -> None:
    html = """<h1 id="activity-name">文章标题</h1><a id="js_name">作者</a>
    <div id="js_content"><p>正文</p><img src="old.jpg" data-src="new.jpg"></div>
    <script>var createTime = '1760000000';</script>"""
    provider = make_provider(tmp_path, html)

    article = provider.get("https://mp.weixin.qq.com/s/token")

    assert article.title == "文章标题"
    assert article.images == ["new.jpg"]
    assert "![](new.jpg)" in article.content_markdown
    assert article.published_at is not None
    assert article.published_at.utcoffset().total_seconds() == 8 * 3600
    assert ArticleCache(tmp_path / "cache").get("https://mp.weixin.qq.com/s/token") == article


@pytest.mark.parametrize(
    ("html", "error"),
    [
        ("<p>环境异常，请完成安全验证</p>", VerificationRequiredError),
        ("<p>内容已被发布者删除</p>", NotFoundError),
    ],
)
def test_200_pages_are_classified_before_parsing(
    tmp_path: Path, html: str, error: type[Exception]
) -> None:
    with pytest.raises(error):
        make_provider(tmp_path, html).get("https://mp.weixin.qq.com/s/token")
