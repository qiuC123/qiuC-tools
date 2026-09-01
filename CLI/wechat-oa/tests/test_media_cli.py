from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from click.utils import strip_ansi
from typer.testing import CliRunner

from wxcli import cli
from wxcli.cli import app
from wxcli.evidence import AccountIdentityEvidence, ArticleEvidence, IdentityStatus
from wxcli.media import ArticleMediaDownloads, build_media_evidence
from wxcli.models import Article, Provider


class FakeMediaCache:
    def __init__(self) -> None:
        self.clear_calls = 0

    def clear(self) -> int:
        self.clear_calls += 1
        return 7


def test_media_cache_clear_uses_only_the_dedicated_cache(monkeypatch) -> None:
    cache = FakeMediaCache()
    monkeypatch.setattr(cli, "default_media_cache", lambda: cache)
    monkeypatch.setattr(
        cli,
        "default_cache",
        lambda: (_ for _ in ()).throw(AssertionError("Article Cache must stay untouched")),
    )
    monkeypatch.setattr(
        cli,
        "default_discovery_store",
        lambda: (_ for _ in ()).throw(AssertionError("Discovery state must stay untouched")),
    )

    result = CliRunner().invoke(app, ["--json", "media", "cache", "clear"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"] == {"cleared": 7}
    assert cache.clear_calls == 1


def test_default_media_cache_is_namespaced_under_runtime_root(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    cache = cli.default_media_cache()

    assert cache.directory == tmp_path / "wxcli" / "media-cache"


def test_media_help_exposes_only_cache_maintenance() -> None:
    result = CliRunner().invoke(app, ["media", "--help"])

    assert result.exit_code == 0
    assert "cache" in result.stdout
    assert "analyze" not in result.stdout


def article_evidence(*, with_image: bool = True) -> ArticleEvidence:
    article = Article(
        title="Campus hiring",
        content_markdown="Apply now",
        source_url="https://mp.weixin.qq.com/s/TOKEN",
        images=["https://mmbiz.qpic.cn/example/640"] if with_image else [],
        provider=Provider.HTTP,
    )
    return ArticleEvidence(
        article=article,
        account_identity=AccountIdentityEvidence(status=IdentityStatus.UNKNOWN),
        last_verified_at=datetime(2026, 1, 1, tzinfo=UTC),
        content_sha256="a" * 64,
        evidence_sha256="b" * 64,
    )


class FakeEvidenceService:
    calls: list[bool] = []

    def __init__(self, provider: object) -> None:
        del provider

    def get(self, url: str, *, no_cache: bool = False) -> ArticleEvidence:
        assert url == "https://mp.weixin.qq.com/s/TOKEN"
        self.calls.append(no_cache)
        return article_evidence()


class FakeArticleMediaDownloader:
    caches: list[object | None] = []

    def __init__(
        self,
        downloader: object,
        *,
        cache: object | None,
        limits: object,
    ) -> None:
        del downloader, limits
        self.caches.append(cache)

    def download(self, article: Article) -> ArticleMediaDownloads:
        assert len(article.images) == 1
        return ArticleMediaDownloads(items=(), total_bytes=0, omitted_count=1)


class FakeArticleMediaAnalyzer:
    def analyze(self, *, source_content_sha256, downloads, configuration):
        return build_media_evidence(
            source_content_sha256=source_content_sha256,
            items=[],
            analysis_started_at=datetime(2026, 1, 1, tzinfo=UTC),
            analysis_finished_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
            configuration=configuration,
            omitted_count=downloads.omitted_count,
        )


def _install_media_cli_fakes(monkeypatch) -> object:
    media_cache = object()
    FakeEvidenceService.calls.clear()
    FakeArticleMediaDownloader.caches.clear()
    monkeypatch.setattr(cli, "default_cache", lambda: object())
    monkeypatch.setattr(cli, "default_media_cache", lambda: media_cache)
    monkeypatch.setattr(cli, "PublicHttpProvider", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "EvidenceService", FakeEvidenceService)
    monkeypatch.setattr(cli, "ArticleMediaDownloader", FakeArticleMediaDownloader)
    monkeypatch.setattr(cli, "ArticleMediaAnalyzer", FakeArticleMediaAnalyzer)
    return media_cache


def test_article_get_keeps_media_analysis_disabled_by_default(monkeypatch) -> None:
    class FakeHttpProvider:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def get(self, url: str, *, no_cache: bool = False) -> dict[str, object]:
            return {"legacy": True, "url": url, "no_cache": no_cache}

    monkeypatch.setattr(cli, "default_cache", lambda: object())
    monkeypatch.setattr(cli, "PublicHttpProvider", FakeHttpProvider)
    monkeypatch.setattr(
        cli,
        "EvidenceService",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("Evidence and media work must remain disabled")
        ),
    )
    monkeypatch.setattr(
        cli,
        "default_media_cache",
        lambda: (_ for _ in ()).throw(AssertionError("Media Cache must stay untouched")),
    )

    result = CliRunner().invoke(
        app,
        ["--json", "article", "get", "https://mp.weixin.qq.com/s/TOKEN"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"] == {
        "legacy": True,
        "url": "https://mp.weixin.qq.com/s/TOKEN",
        "no_cache": False,
    }


def test_explicit_article_media_analysis_returns_linked_schema_v2(monkeypatch) -> None:
    media_cache = _install_media_cli_fakes(monkeypatch)

    result = CliRunner().invoke(
        app,
        [
            "--json",
            "article",
            "get",
            "https://mp.weixin.qq.com/s/TOKEN",
            "--analyze-media",
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data["schema_version"] == "2"
    assert data["article_evidence"]["schema_version"] == "1"
    assert data["media_evidence"]["source_content_sha256"] == "a" * 64
    assert data["media_evidence"]["omitted_count"] == 1
    assert data["media_evidence"]["partial"] is True
    assert FakeEvidenceService.calls == [False]
    assert FakeArticleMediaDownloader.caches == [media_cache]


def test_no_cache_disables_both_article_and_media_cache(monkeypatch) -> None:
    _install_media_cli_fakes(monkeypatch)
    monkeypatch.setattr(
        cli,
        "default_media_cache",
        lambda: (_ for _ in ()).throw(AssertionError("Media Cache must stay disabled")),
    )

    result = CliRunner().invoke(
        app,
        [
            "--json",
            "article",
            "get",
            "https://mp.weixin.qq.com/s/TOKEN",
            "--analyze-media",
            "--no-cache",
        ],
    )

    assert result.exit_code == 0
    assert FakeEvidenceService.calls == [True]
    assert FakeArticleMediaDownloader.caches == [None]


def test_article_evidence_can_explicitly_add_media_evidence(monkeypatch) -> None:
    _install_media_cli_fakes(monkeypatch)

    result = CliRunner().invoke(
        app,
        [
            "--json",
            "article",
            "evidence",
            "https://mp.weixin.qq.com/s/TOKEN",
            "--analyze-media",
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data["schema_version"] == "2"
    assert data["article_evidence"]["content_sha256"] == "a" * 64
    assert data["media_evidence"]["source_content_sha256"] == "a" * 64


def test_article_media_flag_is_explicitly_visible_on_get_and_evidence() -> None:
    runner = CliRunner()

    get_help = runner.invoke(app, ["article", "get", "--help"])
    evidence_help = runner.invoke(app, ["article", "evidence", "--help"])

    assert get_help.exit_code == 0
    assert evidence_help.exit_code == 0
    assert "--analyze-media" in strip_ansi(get_help.stdout)
    assert "--analyze-media" in strip_ansi(evidence_help.stdout)
