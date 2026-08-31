"""Deterministic article-level orchestration for bounded media acquisition."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from wxcli.media.cache import CachedMedia, MediaCache
from wxcli.media.downloader import DownloadedMedia, MediaDownloadFailure
from wxcli.media.models import MediaAnalysisLimits, MediaItemReason
from wxcli.models import Article


class MediaAcquisitionStatus(StrEnum):
    """Internal pre-analysis state for one Article image occurrence."""

    DOWNLOADED = "downloaded"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class MediaAcquisitionItem:
    """Ordered download/cache outcome before QR and OCR analyzers run."""

    index: int
    source_url: str
    status: MediaAcquisitionStatus
    reason: MediaItemReason | None = None
    cache_hit: bool = False
    media: DownloadedMedia | None = None

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError("Media acquisition index cannot be negative.")
        if self.status == MediaAcquisitionStatus.DOWNLOADED:
            if self.media is None or self.reason is not None:
                raise ValueError("Downloaded media requires bytes and cannot have a failure reason.")
            return
        if self.reason is None or self.media is not None or self.cache_hit:
            raise ValueError("Skipped or failed media requires only a reason.")


@dataclass(frozen=True, slots=True)
class ArticleMediaDownloads:
    """Deterministic bounded acquisition results for one Article."""

    items: tuple[MediaAcquisitionItem, ...]
    total_bytes: int
    omitted_count: int = 0

    def __post_init__(self) -> None:
        if self.omitted_count < 0:
            raise ValueError("Media acquisition omitted_count cannot be negative.")
        if [item.index for item in self.items] != list(range(len(self.items))):
            raise ValueError("Media acquisition indexes must be contiguous and ordered from zero.")
        actual_total = sum(
            item.media.byte_length for item in self.items if item.media is not None
        )
        if self.total_bytes != actual_total:
            raise ValueError("Media acquisition total_bytes does not match downloaded items.")

    @property
    def partial(self) -> bool:
        return self.omitted_count > 0 or any(
            item.status != MediaAcquisitionStatus.DOWNLOADED for item in self.items
        )


class BoundedMediaDownloader(Protocol):
    """Minimum safe-downloader interface required by Article orchestration."""

    def download(self, source_url: str, *, max_bytes: int | None = None) -> DownloadedMedia: ...

    def validate_cached(
        self,
        *,
        source_url: str,
        final_url: str,
        content: bytes,
        media_type: str,
        max_bytes: int | None = None,
    ) -> DownloadedMedia: ...


class ArticleMediaDownloader:
    """Acquire Article images in source order under count and total-byte limits."""

    def __init__(
        self,
        downloader: BoundedMediaDownloader,
        *,
        cache: MediaCache | None = None,
        limits: MediaAnalysisLimits | None = None,
    ) -> None:
        self._downloader = downloader
        self._cache = cache
        self._limits = limits or MediaAnalysisLimits()

    def download(self, article: Article) -> ArticleMediaDownloads:
        items: list[MediaAcquisitionItem] = []
        total_bytes = 0
        selected_images = article.images[: self._limits.max_article_images]
        omitted_count = len(article.images) - len(selected_images)
        for index, source_url in enumerate(selected_images):
            remaining = self._limits.max_article_bytes - total_bytes
            if remaining <= 0:
                items.append(_resource_limit_item(index, source_url))
                continue
            per_image_limit = min(self._limits.max_image_bytes, remaining)
            try:
                media, cache_hit = self._from_cache(
                    source_url,
                    max_bytes=per_image_limit,
                )
            except MediaDownloadFailure as failure:
                items.append(
                    _failure_item(
                        index,
                        source_url,
                        _bounded_reason(
                            failure.reason,
                            per_image_limit=per_image_limit,
                            configured_image_limit=self._limits.max_image_bytes,
                        ),
                    )
                )
                continue
            if media is None:
                try:
                    media = self._downloader.download(
                        source_url,
                        max_bytes=per_image_limit,
                    )
                except MediaDownloadFailure as failure:
                    items.append(
                        _failure_item(
                            index,
                            source_url,
                            _bounded_reason(
                                failure.reason,
                                per_image_limit=per_image_limit,
                                configured_image_limit=self._limits.max_image_bytes,
                            ),
                        )
                    )
                    continue
                if self._cache is not None:
                    try:
                        self._cache.put(media)
                    except (OSError, ValueError):
                        pass
            total_bytes += media.byte_length
            items.append(
                MediaAcquisitionItem(
                    index=index,
                    source_url=source_url,
                    status=MediaAcquisitionStatus.DOWNLOADED,
                    cache_hit=cache_hit,
                    media=media,
                )
            )
        return ArticleMediaDownloads(
            items=tuple(items),
            total_bytes=total_bytes,
            omitted_count=omitted_count,
        )

    def _from_cache(
        self,
        source_url: str,
        *,
        max_bytes: int,
    ) -> tuple[DownloadedMedia | None, bool]:
        if self._cache is None:
            return None, False
        try:
            cached = self._cache.get(source_url)
        except (OSError, ValueError):
            return None, False
        if cached is None:
            return None, False
        try:
            return self._validate_cached(cached, max_bytes=max_bytes), True
        except MediaDownloadFailure as failure:
            if failure.reason == MediaItemReason.TOO_LARGE:
                raise
            try:
                self._cache.discard(source_url)
            except (OSError, ValueError):
                pass
            return None, False

    def _validate_cached(self, cached: CachedMedia, *, max_bytes: int) -> DownloadedMedia:
        return self._downloader.validate_cached(
            source_url=cached.source_url,
            final_url=cached.final_url,
            content=cached.content,
            media_type=cached.media_type,
            max_bytes=max_bytes,
        )


_SKIPPED_REASONS = {
    MediaItemReason.BLOCKED_HOST,
    MediaItemReason.UNSAFE_DESTINATION,
    MediaItemReason.RESOURCE_LIMIT,
}


def _failure_item(
    index: int,
    source_url: str,
    reason: MediaItemReason,
) -> MediaAcquisitionItem:
    status = (
        MediaAcquisitionStatus.SKIPPED
        if reason in _SKIPPED_REASONS
        else MediaAcquisitionStatus.FAILED
    )
    return MediaAcquisitionItem(
        index=index,
        source_url=source_url,
        status=status,
        reason=reason,
    )


def _resource_limit_item(index: int, source_url: str) -> MediaAcquisitionItem:
    return _failure_item(index, source_url, MediaItemReason.RESOURCE_LIMIT)


def _bounded_reason(
    reason: MediaItemReason,
    *,
    per_image_limit: int,
    configured_image_limit: int,
) -> MediaItemReason:
    if reason == MediaItemReason.TOO_LARGE and per_image_limit < configured_image_limit:
        return MediaItemReason.RESOURCE_LIMIT
    return reason
