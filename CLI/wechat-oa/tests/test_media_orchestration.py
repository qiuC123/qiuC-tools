from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from wxcli.media import (
    ArticleMediaDownloader,
    DownloadedMedia,
    MediaAcquisitionItem,
    MediaAcquisitionStatus,
    MediaAnalysisLimits,
    MediaCache,
    MediaDownloadFailure,
    MediaFormat,
    MediaItemReason,
)
from wxcli.models import Article, Provider


class FakeDownloader:
    def __init__(
        self,
        responses: dict[str, DownloadedMedia | MediaItemReason] | None = None,
        *,
        reject_cached: bool = False,
    ) -> None:
        self.responses = responses or {}
        self.reject_cached = reject_cached
        self.calls: list[tuple[str, int | None]] = []
        self.cached_calls: list[tuple[str, int | None]] = []

    def download(self, source_url: str, *, max_bytes: int | None = None) -> DownloadedMedia:
        self.calls.append((source_url, max_bytes))
        response = self.responses[source_url]
        if isinstance(response, MediaItemReason):
            raise MediaDownloadFailure(response, "synthetic failure")
        if max_bytes is not None and response.byte_length > max_bytes:
            raise MediaDownloadFailure(MediaItemReason.TOO_LARGE, "synthetic limit")
        return response

    def validate_cached(
        self,
        *,
        source_url: str,
        final_url: str,
        content: bytes,
        media_type: str,
        max_bytes: int | None = None,
    ) -> DownloadedMedia:
        self.cached_calls.append((source_url, max_bytes))
        if self.reject_cached:
            raise MediaDownloadFailure(MediaItemReason.MALFORMED_IMAGE, "synthetic corrupt cache")
        if max_bytes is not None and len(content) > max_bytes:
            raise MediaDownloadFailure(MediaItemReason.TOO_LARGE, "synthetic cached limit")
        return downloaded(source_url, content, final_url=final_url, media_type=media_type)


def downloaded(
    url: str,
    content: bytes,
    *,
    final_url: str | None = None,
    media_type: str = "image/png",
) -> DownloadedMedia:
    return DownloadedMedia(
        source_url=url,
        final_url=final_url or url,
        content=content,
        byte_sha256=hashlib.sha256(content).hexdigest(),
        media_format=MediaFormat.PNG,
        media_type=media_type,
        byte_length=len(content),
        width=2,
        height=2,
        redirect_urls=(),
    )


def article(*images: str) -> Article:
    return Article(
        title="Synthetic article",
        content_markdown="body",
        images=list(images),
        provider=Provider.LOCAL,
    )


def test_article_download_preserves_order_and_isolates_item_failures() -> None:
    first = "https://mmbiz.qpic.cn/first/640"
    blocked = "https://example.com/blocked.png"
    failed = "https://mmbiz.qpic.cn/failed/640"
    last = "https://mmbiz.qpic.cn/last/640"
    fetcher = FakeDownloader(
        {
            first: downloaded(first, b"first"),
            blocked: MediaItemReason.BLOCKED_HOST,
            failed: MediaItemReason.DOWNLOAD_FAILED,
            last: downloaded(last, b"last"),
        }
    )

    result = ArticleMediaDownloader(fetcher).download(article(first, blocked, failed, last))

    assert [item.index for item in result.items] == [0, 1, 2, 3]
    assert [item.status for item in result.items] == [
        MediaAcquisitionStatus.DOWNLOADED,
        MediaAcquisitionStatus.SKIPPED,
        MediaAcquisitionStatus.FAILED,
        MediaAcquisitionStatus.DOWNLOADED,
    ]
    assert result.items[1].reason == MediaItemReason.BLOCKED_HOST
    assert result.items[2].reason == MediaItemReason.DOWNLOAD_FAILED
    assert result.total_bytes == 9
    assert result.partial is True


def test_article_image_count_limit_skips_without_calling_downloader() -> None:
    urls = tuple(f"https://mmbiz.qpic.cn/{index}/640" for index in range(4))
    fetcher = FakeDownloader({url: downloaded(url, b"x") for url in urls})
    limits = MediaAnalysisLimits(max_article_images=2)

    result = ArticleMediaDownloader(fetcher, limits=limits).download(article(*urls))

    assert fetcher.calls == [(urls[0], 10 * 1024 * 1024), (urls[1], 10 * 1024 * 1024)]
    assert len(result.items) == 2
    assert result.omitted_count == 2
    assert result.partial is True


def test_article_total_byte_limit_is_passed_to_each_download() -> None:
    urls = tuple(f"https://mmbiz.qpic.cn/{index}/640" for index in range(4))
    fetcher = FakeDownloader(
        {
            urls[0]: downloaded(urls[0], b"a" * 6),
            urls[1]: downloaded(urls[1], b"b" * 5),
            urls[2]: downloaded(urls[2], b"c" * 4),
            urls[3]: downloaded(urls[3], b"d"),
        }
    )
    limits = MediaAnalysisLimits(
        max_image_bytes=8,
        max_article_bytes=10,
    )

    result = ArticleMediaDownloader(fetcher, limits=limits).download(article(*urls))

    assert fetcher.calls == [(urls[0], 8), (urls[1], 4), (urls[2], 4)]
    assert [item.status for item in result.items] == [
        MediaAcquisitionStatus.DOWNLOADED,
        MediaAcquisitionStatus.SKIPPED,
        MediaAcquisitionStatus.DOWNLOADED,
        MediaAcquisitionStatus.SKIPPED,
    ]
    assert result.items[1].reason == MediaItemReason.RESOURCE_LIMIT
    assert result.items[3].reason == MediaItemReason.RESOURCE_LIMIT
    assert result.total_bytes == 10


def test_per_image_limit_remains_too_large_when_article_budget_is_available() -> None:
    url = "https://mmbiz.qpic.cn/large/640"
    fetcher = FakeDownloader({url: downloaded(url, b"x" * 9)})
    limits = MediaAnalysisLimits(max_image_bytes=8, max_article_bytes=100)

    result = ArticleMediaDownloader(fetcher, limits=limits).download(article(url))

    assert result.items[0].status == MediaAcquisitionStatus.FAILED
    assert result.items[0].reason == MediaItemReason.TOO_LARGE


def test_repeated_url_uses_cache_but_each_occurrence_counts_toward_article_bytes(
    tmp_path: Path,
) -> None:
    url = "https://mmbiz.qpic.cn/repeated/640"
    fetcher = FakeDownloader({url: downloaded(url, b"same")})
    cache = MediaCache(tmp_path / "media")

    result = ArticleMediaDownloader(fetcher, cache=cache).download(article(url, url))

    assert fetcher.calls == [(url, 10 * 1024 * 1024)]
    assert fetcher.cached_calls == [(url, 10 * 1024 * 1024)]
    assert [item.cache_hit for item in result.items] == [False, True]
    assert result.total_bytes == 8


def test_cache_hit_avoids_network_across_article_runs(tmp_path: Path) -> None:
    url = "https://mmbiz.qpic.cn/cached/640"
    cache = MediaCache(tmp_path / "media")
    first_fetcher = FakeDownloader({url: downloaded(url, b"cached bytes")})
    ArticleMediaDownloader(first_fetcher, cache=cache).download(article(url))
    second_fetcher = FakeDownloader()

    result = ArticleMediaDownloader(second_fetcher, cache=cache).download(article(url))

    assert second_fetcher.calls == []
    assert len(second_fetcher.cached_calls) == 1
    assert result.items[0].cache_hit is True
    assert result.items[0].media is not None
    assert result.items[0].media.content == b"cached bytes"


def test_invalid_cached_raster_is_discarded_and_downloaded_again(tmp_path: Path) -> None:
    url = "https://mmbiz.qpic.cn/invalid-cache/640"
    cache = MediaCache(tmp_path / "media")
    cache.put(downloaded(url, b"locally invalid but hash-consistent"))
    replacement = downloaded(url, b"fresh valid bytes")
    fetcher = FakeDownloader({url: replacement}, reject_cached=True)

    result = ArticleMediaDownloader(fetcher, cache=cache).download(article(url))

    assert fetcher.calls == [(url, 10 * 1024 * 1024)]
    assert result.items[0].cache_hit is False
    assert result.items[0].media == replacement
    cached = cache.get(url)
    assert cached is not None
    assert cached.content == replacement.content


def test_cached_item_over_remaining_budget_fails_without_network_or_eviction(
    tmp_path: Path,
) -> None:
    first = "https://mmbiz.qpic.cn/first/640"
    cached_url = "https://mmbiz.qpic.cn/cached-large/640"
    cache = MediaCache(tmp_path / "media")
    cache.put(downloaded(cached_url, b"b" * 5))
    fetcher = FakeDownloader({first: downloaded(first, b"a" * 6)})
    limits = MediaAnalysisLimits(max_image_bytes=8, max_article_bytes=10)

    result = ArticleMediaDownloader(fetcher, cache=cache, limits=limits).download(
        article(first, cached_url)
    )

    assert fetcher.calls == [(first, 8)]
    assert result.items[1].status == MediaAcquisitionStatus.SKIPPED
    assert result.items[1].reason == MediaItemReason.RESOURCE_LIMIT
    assert cache.get(cached_url) is not None


def test_cache_io_failure_falls_back_to_network_without_losing_media(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://mmbiz.qpic.cn/cache-io/640"
    cache = MediaCache(tmp_path / "media")
    fetcher = FakeDownloader({url: downloaded(url, b"network bytes")})

    def fail_get(_: str) -> None:
        raise OSError("synthetic cache failure")

    monkeypatch.setattr(cache, "get", fail_get)

    result = ArticleMediaDownloader(fetcher, cache=cache).download(article(url))

    assert result.items[0].status == MediaAcquisitionStatus.DOWNLOADED
    assert result.items[0].cache_hit is False
    assert fetcher.calls == [(url, 10 * 1024 * 1024)]


def test_cache_write_failure_does_not_discard_successful_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://mmbiz.qpic.cn/cache-write/640"
    cache = MediaCache(tmp_path / "media")
    fetcher = FakeDownloader({url: downloaded(url, b"network bytes")})

    def fail_put(_: DownloadedMedia) -> None:
        raise OSError("synthetic cache failure")

    monkeypatch.setattr(cache, "put", fail_put)

    result = ArticleMediaDownloader(fetcher, cache=cache).download(article(url))

    assert result.items[0].status == MediaAcquisitionStatus.DOWNLOADED
    assert result.items[0].media is not None
    assert result.items[0].media.content == b"network bytes"


def test_acquisition_models_reject_inconsistent_states() -> None:
    url = "https://mmbiz.qpic.cn/example/640"
    with pytest.raises(ValueError, match="requires bytes"):
        MediaAcquisitionItem(
            index=0,
            source_url=url,
            status=MediaAcquisitionStatus.DOWNLOADED,
        )
    valid = MediaAcquisitionItem(
        index=0,
        source_url=url,
        status=MediaAcquisitionStatus.DOWNLOADED,
        media=downloaded(url, b"x"),
    )
    with pytest.raises(ValueError, match="total_bytes"):
        replace(
            ArticleMediaDownloader(FakeDownloader()).download(article()),
            items=(valid,),
            total_bytes=0,
        )
    with pytest.raises(ValueError, match="omitted_count"):
        replace(
            ArticleMediaDownloader(FakeDownloader()).download(article()),
            omitted_count=-1,
        )
