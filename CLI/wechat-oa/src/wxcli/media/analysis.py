"""Deterministic article-level orchestration for bounded local media analysis."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from wxcli.media.downloader import DownloadedMedia
from wxcli.media.models import (
    MAX_IMAGE_BYTES,
    MAX_IMAGE_PIXELS,
    MediaAnalysisConfiguration,
    MediaEvidence,
    MediaFormat,
    MediaItemEvidence,
    MediaItemReason,
    MediaItemStatus,
    OCREvidence,
    OCRStatus,
    QREvidence,
    QRStatus,
    build_media_evidence,
)
from wxcli.media.ocr import OCRProvider, WindowsOCRProvider
from wxcli.media.orchestration import (
    ArticleMediaDownloads,
    MediaAcquisitionItem,
    MediaAcquisitionStatus,
)
from wxcli.media.qr import StandardQRAnalyzer

ANALYSIS_GUARD = "media-analysis-guard"
ANALYSIS_GUARD_VERSION = "1"

_FORMAT_MEDIA_TYPES = {
    MediaFormat.JPEG: "image/jpeg",
    MediaFormat.PNG: "image/png",
    MediaFormat.WEBP: "image/webp",
    MediaFormat.GIF: "image/gif",
}


class QRAnalyzer(Protocol):
    """Replaceable QR boundary consumed by article media analysis."""

    def analyze(self, media: DownloadedMedia) -> QREvidence: ...


class ArticleMediaAnalyzer:
    """Analyze bounded downloads once per byte hash and preserve every occurrence."""

    def __init__(
        self,
        qr_analyzer: QRAnalyzer | None = None,
        ocr_provider: OCRProvider | None = None,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._qr_analyzer = qr_analyzer or StandardQRAnalyzer()
        self._ocr_provider = ocr_provider or WindowsOCRProvider()
        self._now = now or (lambda: datetime.now(UTC))

    def analyze(
        self,
        *,
        source_content_sha256: str,
        downloads: ArticleMediaDownloads,
        configuration: MediaAnalysisConfiguration | None = None,
    ) -> MediaEvidence:
        """Produce linked Media Evidence without network or credential access."""
        actual_configuration = configuration or MediaAnalysisConfiguration()
        started_at = self._now()
        analyzed: dict[str, tuple[QREvidence, OCREvidence]] = {}
        items: list[MediaItemEvidence] = []
        remaining_ocr_characters = (
            actual_configuration.limits.max_ocr_characters_per_batch
        )

        for acquisition in downloads.items:
            if acquisition.status != MediaAcquisitionStatus.DOWNLOADED:
                items.append(_unavailable_item(acquisition))
                continue
            assert acquisition.media is not None
            media = acquisition.media
            if not _is_validated_media(media):
                items.append(_invalid_media_item(acquisition))
                continue
            if media.byte_sha256 not in analyzed:
                analyzed[media.byte_sha256] = self._analyze_once(
                    media,
                    requested_language=actual_configuration.ocr_language,
                )
            qr, raw_ocr = analyzed[media.byte_sha256]
            ocr, consumed = _bound_ocr_output(raw_ocr, remaining_ocr_characters)
            remaining_ocr_characters -= consumed
            items.append(_analyzed_item(acquisition, qr=qr, ocr=ocr))

        return build_media_evidence(
            source_content_sha256=source_content_sha256,
            items=items,
            analysis_started_at=started_at,
            analysis_finished_at=self._now(),
            configuration=actual_configuration,
            omitted_count=downloads.omitted_count,
        )

    def _analyze_once(
        self,
        media: DownloadedMedia,
        *,
        requested_language: str,
    ) -> tuple[QREvidence, OCREvidence]:
        try:
            qr = self._qr_analyzer.analyze(media)
            if qr.source_byte_sha256 != media.byte_sha256:
                qr = _failed_qr(media.byte_sha256)
        except Exception:
            qr = _failed_qr(media.byte_sha256)

        try:
            ocr = self._ocr_provider.analyze(
                media,
                requested_language=requested_language,
            )
            if (
                ocr.source_byte_sha256 != media.byte_sha256
                or ocr.requested_language != requested_language
            ):
                ocr = _failed_ocr(media.byte_sha256, requested_language)
        except Exception:
            ocr = _failed_ocr(media.byte_sha256, requested_language)
        return qr, ocr


def _is_validated_media(media: DownloadedMedia) -> bool:
    return (
        1 <= media.byte_length <= MAX_IMAGE_BYTES
        and media.byte_length == len(media.content)
        and hashlib.sha256(media.content).hexdigest() == media.byte_sha256
        and media.width >= 1
        and media.height >= 1
        and media.width * media.height <= MAX_IMAGE_PIXELS
        and media.media_type == _FORMAT_MEDIA_TYPES.get(media.media_format)
    )


def _unavailable_item(acquisition: MediaAcquisitionItem) -> MediaItemEvidence:
    assert acquisition.reason is not None
    status = (
        MediaItemStatus.SKIPPED
        if acquisition.status == MediaAcquisitionStatus.SKIPPED
        else MediaItemStatus.FAILED
    )
    return MediaItemEvidence(
        index=acquisition.index,
        source_url=acquisition.source_url,
        status=status,
        reason=acquisition.reason,
    )


def _invalid_media_item(acquisition: MediaAcquisitionItem) -> MediaItemEvidence:
    return MediaItemEvidence(
        index=acquisition.index,
        source_url=acquisition.source_url,
        status=MediaItemStatus.FAILED,
        reason=MediaItemReason.MALFORMED_IMAGE,
    )


def _analyzed_item(
    acquisition: MediaAcquisitionItem,
    *,
    qr: QREvidence,
    ocr: OCREvidence,
) -> MediaItemEvidence:
    assert acquisition.media is not None
    media = acquisition.media
    return MediaItemEvidence(
        index=acquisition.index,
        source_url=acquisition.source_url,
        status=MediaItemStatus.ANALYZED,
        cache_hit=acquisition.cache_hit,
        byte_sha256=media.byte_sha256,
        media_format=media.media_format,
        media_type=media.media_type,
        byte_length=media.byte_length,
        width=media.width,
        height=media.height,
        qr=qr,
        ocr=ocr,
    )


def _failed_qr(source_byte_sha256: str) -> QREvidence:
    return QREvidence(
        source_byte_sha256=source_byte_sha256,
        analyzer=ANALYSIS_GUARD,
        analyzer_version=ANALYSIS_GUARD_VERSION,
        status=QRStatus.FAILED,
    )


def _failed_ocr(source_byte_sha256: str, requested_language: str) -> OCREvidence:
    return OCREvidence(
        source_byte_sha256=source_byte_sha256,
        analyzer=ANALYSIS_GUARD,
        analyzer_version=ANALYSIS_GUARD_VERSION,
        status=OCRStatus.FAILED,
        requested_language=requested_language,
    )


def _bound_ocr_output(
    evidence: OCREvidence,
    remaining_characters: int,
) -> tuple[OCREvidence, int]:
    if evidence.status != OCRStatus.ANALYZED:
        return evidence, 0
    assert evidence.text is not None
    bounded_text = evidence.text[:remaining_characters]
    bounded = OCREvidence.model_validate(
        evidence.model_dump()
        | {
            "text": bounded_text,
            "truncated": evidence.truncated or len(bounded_text) < len(evidence.text),
        }
    )
    return bounded, len(bounded_text)
