from datetime import UTC, datetime

import pytest

from wxcli.errors import ErrorCode, WxcliError
from wxcli.evidence import (
    EvidenceService,
    ExpectedAccount,
    IdentityStatus,
    build_article_evidence,
)
from wxcli.models import Provider
from wxcli.public_article import PublicArticleParser


def article_html(*, include_date: bool = True, biz_script: str = "") -> str:
    date_script = "var ct = '1735689600';" if include_date else ""
    return f"""
    <html><head><meta property="og:title" content="Fallback title"></head><body>
      <h1 id="activity-name">Campus hiring</h1>
      <a id="js_name" href="https://mp.weixin.qq.com/mp/profile_ext?__biz=profile-biz">Acme Jobs</a>
      <div id="js_content">
        <p>Hello <a href="HTTPS://Jobs.Example.COM/apply#section">apply</a></p>
        <a href="mailto:jobs@example.com">mail</a>
        <a href="tel:+86123">phone</a>
        <a href="https://mp.weixin.qq.com/s/another">related</a>
        <a href="javascript:alert(1)">bad</a>
        <a href="https://user:pass@example.com/private">bad auth</a>
        <img data-src="https://img.example.com/a.jpg">
      </div>
      <script>{date_script}{biz_script}</script>
    </body></html>
    """


def test_parser_collects_identity_links_images_and_source_date() -> None:
    document = PublicArticleParser.parse(
        article_html(),
        "https://mp.weixin.qq.com/s?__biz=query-biz&mid=123&idx=1",
        Provider.HTTP,
    )

    assert document.account_display_name == "Acme Jobs"
    assert document.account_biz_id == "query-biz"
    assert document.article.published_at is not None
    assert document.article.images == ["https://img.example.com/a.jpg"]
    assert [link.kind for link in document.external_links] == [
        "external_http",
        "email",
        "phone",
        "wechat",
    ]
    assert document.external_links[0].normalized_value == "https://jobs.example.com/apply"


def test_biz_id_falls_back_to_profile_then_safe_script() -> None:
    profile = PublicArticleParser.parse(
        article_html(), "https://mp.weixin.qq.com/s/TOKEN", Provider.HTTP
    )
    assert profile.account_biz_id == "profile-biz"

    without_profile = article_html().replace(
        '<a id="js_name" href="https://mp.weixin.qq.com/mp/profile_ext?__biz=profile-biz">',
        '<span id="js_name">',
    ).replace("</a>\n      <div", "</span>\n      <div", 1)
    scripted = PublicArticleParser.parse(
        without_profile.replace("<script>", "<script>var __biz='script-biz';"),
        "https://mp.weixin.qq.com/s/TOKEN",
        Provider.HTTP,
    )
    assert scripted.account_biz_id == "script-biz"


def test_article_body_cannot_spoof_identity_or_published_date() -> None:
    html = """
      <h1 id="activity-name">Campus hiring</h1>
      <span id="js_name">Unverified account</span>
      <div id="js_content">
        <pre>var __biz='EXPECTED_BIZ'; var ct='1735689600';</pre>
        <a id="js_name" href="https://mp.weixin.qq.com/mp/profile_ext?__biz=EXPECTED_BIZ">
          spoofed profile
        </a>
      </div>
    """

    document = PublicArticleParser.parse(
        html, "https://mp.weixin.qq.com/s/TOKEN", Provider.HTTP
    )
    evidence = build_article_evidence(
        document, [ExpectedAccount(biz_id="EXPECTED_BIZ")]
    )

    assert document.account_biz_id is None
    assert document.article.published_at is None
    assert evidence.account_identity.status == IdentityStatus.MISMATCH


def test_missing_required_article_nodes_is_parsing_error() -> None:
    with pytest.raises(WxcliError) as raised:
        PublicArticleParser.parse("<html></html>", "https://mp.weixin.qq.com/s/T", Provider.HTTP)

    assert raised.value.code == ErrorCode.PARSING_ERROR


def test_empty_article_shell_is_parsing_error() -> None:
    html = '<h1 id="activity-name">Campus hiring</h1><div id="js_content"></div>'

    with pytest.raises(WxcliError) as raised:
        PublicArticleParser.parse(html, "https://mp.weixin.qq.com/s/T", Provider.HTTP)

    assert raised.value.code == ErrorCode.PARSING_ERROR
    assert "no extractable content" in raised.value.message


def test_image_only_article_is_still_extractable() -> None:
    html = """
      <h1 id="activity-name">Campus hiring poster</h1>
      <div id="js_content"><img data-src="https://img.example.com/poster.jpg"></div>
    """

    document = PublicArticleParser.parse(
        html, "https://mp.weixin.qq.com/s/T", Provider.HTTP
    )

    assert document.article.images == ["https://img.example.com/poster.jpg"]
    assert "poster.jpg" in document.article.content_markdown


def test_parser_collects_extended_static_media_and_merges_runtime_observations() -> None:
    html = """
      <h1 id="activity-name">Campus hiring poster</h1>
      <div id="js_content">
        <img data-original="https://mmbiz.qpic.cn/image.jpg">
        <picture><source srcset="https://mmbiz.qpic.cn/a.webp 1x, https://mmbiz.qpic.cn/b.webp 2x"></picture>
        <svg><image xlink:href="https://mmbiz.qpic.cn/vector.png"></image></svg>
        <video poster="https://mmbiz.qpic.cn/poster.jpg"></video>
        <div style="background: url(https://mmbiz.qpic.cn/background.jpg)"></div>
      </div>
    """

    document = PublicArticleParser.parse(
        html,
        "https://mp.weixin.qq.com/s/T",
        Provider.CHROME,
        observed_images=[
            "https://mmbiz.qpic.cn/runtime.jpg",
            "https://mmbiz.qpic.cn/image.jpg",
        ],
    )

    assert document.article.images == [
        "https://mmbiz.qpic.cn/runtime.jpg",
        "https://mmbiz.qpic.cn/image.jpg",
        "https://mmbiz.qpic.cn/a.webp",
        "https://mmbiz.qpic.cn/b.webp",
        "https://mmbiz.qpic.cn/vector.png",
        "https://mmbiz.qpic.cn/poster.jpg",
        "https://mmbiz.qpic.cn/background.jpg",
    ]


@pytest.mark.parametrize(
    ("expected", "status"),
    [
        (ExpectedAccount(biz_id="query-biz"), IdentityStatus.ALLOWLIST_MATCHED),
        (ExpectedAccount(display_names=[" Acme Jobs "]), IdentityStatus.NAME_ONLY_MATCHED),
        (ExpectedAccount(biz_id="different"), IdentityStatus.MISMATCH),
    ],
)
def test_evidence_identity_matching(expected: ExpectedAccount, status: IdentityStatus) -> None:
    document = PublicArticleParser.parse(
        article_html(),
        "https://mp.weixin.qq.com/s?__biz=query-biz&mid=123",
        Provider.HTTP,
    )

    evidence = build_article_evidence(document, [expected])

    assert evidence.account_identity.status == status


def test_evidence_hashes_ignore_observation_time_but_date_may_be_missing() -> None:
    document = PublicArticleParser.parse(
        article_html(include_date=False), "https://mp.weixin.qq.com/s/TOKEN", Provider.HTTP
    )
    first = build_article_evidence(document, [], now=datetime(2026, 1, 1, tzinfo=UTC))
    second = build_article_evidence(document, [], now=datetime(2026, 2, 1, tzinfo=UTC))

    assert first.article.published_at is None
    assert first.content_sha256 == second.content_sha256
    assert first.evidence_sha256 == second.evidence_sha256
    assert first.last_verified_at != second.last_verified_at
    assert first.account_identity.status == IdentityStatus.OBSERVED


def test_expected_account_rejects_empty_identity() -> None:
    with pytest.raises(ValueError):
        ExpectedAccount(display_names=[" "])
    with pytest.raises(ValueError):
        ExpectedAccount(display_names=["x" * 201])
    with pytest.raises(ValueError):
        ExpectedAccount(display_names=["Acme\nJobs"])


def test_evidence_service_and_unknown_identity() -> None:
    html = article_html().replace(
        '<a id="js_name" href="https://mp.weixin.qq.com/mp/profile_ext?__biz=profile-biz">Acme Jobs</a>',
        "",
    )
    document = PublicArticleParser.parse(html, "https://mp.weixin.qq.com/s/T", Provider.HTTP)

    class ProviderStub:
        def get_document(self, url: str):
            assert url.endswith("/T")
            return document

    result = EvidenceService(ProviderStub()).get("https://mp.weixin.qq.com/s/T")
    assert result.account_identity.status == IdentityStatus.UNKNOWN


def test_evidence_service_forwards_explicit_no_cache_only_when_requested() -> None:
    document = PublicArticleParser.parse(
        article_html(),
        "https://mp.weixin.qq.com/s/T",
        Provider.HTTP,
    )
    calls: list[bool] = []

    class ProviderStub:
        def get_document(self, url: str, *, no_cache: bool = False):
            calls.append(no_cache)
            return document

    service = EvidenceService(ProviderStub())
    service.get("https://mp.weixin.qq.com/s/T")
    service.get("https://mp.weixin.qq.com/s/T", no_cache=True)

    assert calls == [False, True]


def test_link_normalization_handles_bad_ports_empty_contacts_and_explicit_ports() -> None:
    assert PublicArticleParser._normalize_link("https://example.com:bad/x") is None
    assert PublicArticleParser._normalize_link("mailto:") is None
    assert PublicArticleParser._normalize_link("https://example.com:443/x") == "https://example.com:443/x"
