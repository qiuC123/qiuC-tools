"""Mocked tests for public HTTP page classification, parsing, and cache behavior."""

from pathlib import Path

import httpx
import pytest

from wxcli.cache import ArticleCache
from wxcli.errors import ErrorCode, NotFoundError, VerificationRequiredError, WxcliError
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


def test_complete_article_is_not_reclassified_by_words_inside_its_body(tmp_path: Path) -> None:
    html = """
      <h1 id="activity-name">正常文章</h1>
      <div id="js_content"><p>正文讨论验证码，也提到了“页面不存在”的错误提示。</p></div>
    """

    article = make_provider(tmp_path, html).get("https://mp.weixin.qq.com/s/token")

    assert article.title == "正常文章"


def test_verification_marker_outside_an_article_shell_still_wins(tmp_path: Path) -> None:
    html = """
      <p>环境异常，请完成安全验证</p>
      <h1 id="activity-name">残留标题</h1>
      <div id="js_content"><p>残留正文</p></div>
    """

    with pytest.raises(VerificationRequiredError):
        make_provider(tmp_path, html).get("https://mp.weixin.qq.com/s/token")


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


def test_evidence_document_always_fetches_fresh_html_instead_of_article_cache(tmp_path: Path) -> None:
    cached = make_provider(
        tmp_path,
        '<h1 id="activity-name">old</h1><div id="js_content"><p>old</p></div>',
    )
    cached.get("https://mp.weixin.qq.com/s/token")
    fresh = make_provider(
        tmp_path,
        '<h1 id="activity-name">new</h1><div id="js_content"><p>new</p></div>',
    )

    assert fresh.get("https://mp.weixin.qq.com/s/token").title == "old"
    assert fresh.get_document("https://mp.weixin.qq.com/s/token").article.title == "new"


def test_http_provider_allows_only_internal_redirects(tmp_path: Path) -> None:
    article = '<h1 id="activity-name">new</h1><div id="js_content"><p>new</p></div>'

    def internal(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/s/old":
            return httpx.Response(302, headers={"Location": "/s/new"})
        return httpx.Response(200, text=article)

    provider = PublicHttpProvider(httpx.Client(transport=httpx.MockTransport(internal)))
    assert provider.get("https://mp.weixin.qq.com/s/old").title == "new"

    external = PublicHttpProvider(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(302, headers={"Location": "https://example.com/wrapper"})
            )
        )
    )
    with pytest.raises(WxcliError) as raised:
        external.get("https://mp.weixin.qq.com/s/old")
    assert raised.value.code == ErrorCode.NETWORK_ERROR
