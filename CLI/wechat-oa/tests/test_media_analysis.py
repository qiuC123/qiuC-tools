import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from wxcli.media import (
    ANALYSIS_GUARD,
    ArticleMediaAnalyzer,
    ArticleMediaDownloads,
    DownloadedMedia,
    MediaAcquisitionItem,
    MediaAcquisitionStatus,
    MediaAnalysisConfiguration,
    MediaAnalysisLimits,
    MediaFormat,
    MediaItemReason,
    MediaItemStatus,
    OCREvidence,
    OCRStatus,
    QREvidence,
    QRPayloadEvidence,
    QRPayloadType,
    QRStatus,
)


def downloaded(
    content: bytes,
    *,
    source_url: str = "https://mmbiz.qpic.cn/example/0",
) -> DownloadedMedia:
    return DownloadedMedia(
        source_url=source_url,
        final_url=source_url,
        content=content,
        byte_sha256=hashlib.sha256(content).hexdigest(),
        media_format=MediaFormat.PNG,
        media_type="image/png",
        byte_length=len(content),
        width=10,
        height=10,
        redirect_urls=(),
    )


def acquired(
    *media: DownloadedMedia,
    cache_hits: tuple[bool, ...] | None = None,
    omitted_count: int = 0,
) -> ArticleMediaDownloads:
    hits = cache_hits or tuple(False for _ in media)
    return ArticleMediaDownloads(
        items=tuple(
            MediaAcquisitionItem(
                index=index,
                source_url=value.source_url,
                status=MediaAcquisitionStatus.DOWNLOADED,
                cache_hit=hits[index],
                media=value,
            )
            for index, value in enumerate(media)
        ),
        total_bytes=sum(value.byte_length for value in media),
        omitted_count=omitted_count,
    )


class FakeQRAnalyzer:
    def __init__(self, *, fail: bool = False, wrong_hash: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail
        self.wrong_hash = wrong_hash

    def analyze(self, media: DownloadedMedia) -> QREvidence:
        self.calls.append(media.byte_sha256)
        if self.fail:
            raise RuntimeError("private decoder detail")
        source_hash = "f" * 64 if self.wrong_hash else media.byte_sha256
        return QREvidence(
            source_byte_sha256=source_hash,
            analyzer="fake-qr",
            analyzer_version="1",
            status=QRStatus.NOT_FOUND,
        )


class PayloadQRAnalyzer(FakeQRAnalyzer):
    def analyze(self, media: DownloadedMedia) -> QREvidence:
        self.calls.append(media.byte_sha256)
        return QREvidence(
            source_byte_sha256=media.byte_sha256,
            analyzer="fake-qr",
            analyzer_version="1",
            status=QRStatus.DECODED,
            payloads=(
                QRPayloadEvidence.from_payload(
                    index=0,
                    payload_type=QRPayloadType.TEXT,
                    payload="abc",
                ),
                QRPayloadEvidence.from_payload(
                    index=1,
                    payload_type=QRPayloadType.TEXT,
                    payload="def",
                ),
            ),
        )


class FakeOCRProvider:
    def __init__(
        self,
        *,
        text: str = "校园招聘",
        status: OCRStatus = OCRStatus.ANALYZED,
        fail: bool = False,
        wrong_hash: bool = False,
        wrong_language: bool = False,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.text = text
        self.status = status
        self.fail = fail
        self.wrong_hash = wrong_hash
        self.wrong_language = wrong_language

    def analyze(
        self,
        media: DownloadedMedia,
        *,
        requested_language: str = "zh-Hans",
    ) -> OCREvidence:
        self.calls.append((media.byte_sha256, requested_language))
        if self.fail:
            raise RuntimeError("private OCR detail")
        source_hash = "f" * 64 if self.wrong_hash else media.byte_sha256
        language = "en-US" if self.wrong_language else requested_language
        return OCREvidence(
            source_byte_sha256=source_hash,
            analyzer="fake-ocr",
            analyzer_version="1",
            status=self.status,
            requested_language=language,
            text=self.text if self.status == OCRStatus.ANALYZED else None,
        )


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(seconds=1)
        return current


def analyze(
    downloads: ArticleMediaDownloads,
    *,
    qr: FakeQRAnalyzer | None = None,
    ocr: FakeOCRProvider | None = None,
    configuration: MediaAnalysisConfiguration | None = None,
):
    actual_qr = qr or FakeQRAnalyzer()
    actual_ocr = ocr or FakeOCRProvider()
    evidence = ArticleMediaAnalyzer(
        actual_qr,
        actual_ocr,
        now=Clock(),
    ).analyze(
        source_content_sha256="a" * 64,
        downloads=downloads,
        configuration=configuration,
    )
    return evidence, actual_qr, actual_ocr


def test_analysis_accepts_a_stricter_invocation_ocr_budget() -> None:
    evidence = ArticleMediaAnalyzer(
        FakeQRAnalyzer(),
        FakeOCRProvider(text="校园招聘"),
        now=Clock(),
    ).analyze(
        source_content_sha256="a" * 64,
        downloads=acquired(downloaded(b"image")),
        ocr_character_budget=2,
    )

    assert evidence.items[0].ocr is not None
    assert evidence.items[0].ocr.text == "校园"
    assert evidence.items[0].ocr.truncated is True


def test_analysis_rejects_an_ocr_budget_above_the_recorded_configuration() -> None:
    with pytest.raises(ValueError, match="OCR character budget"):
        ArticleMediaAnalyzer(
            FakeQRAnalyzer(),
            FakeOCRProvider(),
            now=Clock(),
        ).analyze(
            source_content_sha256="a" * 64,
            downloads=acquired(),
            ocr_character_budget=1_000_001,
        )


def test_analysis_preserves_occurrences_but_deduplicates_identical_bytes() -> None:
    first = downloaded(b"same", source_url="https://mmbiz.qpic.cn/example/first")
    second = downloaded(b"same", source_url="https://mmbiz.qpic.cn/example/second")

    evidence, qr, ocr = analyze(
        acquired(first, second, cache_hits=(False, True))
    )

    assert len(qr.calls) == 1
    assert len(ocr.calls) == 1
    assert [item.source_url for item in evidence.items] == [
        first.source_url,
        second.source_url,
    ]
    assert [item.cache_hit for item in evidence.items] == [False, True]
    assert evidence.items[0].qr == evidence.items[1].qr
    assert evidence.items[0].ocr == evidence.items[1].ocr
    assert evidence.summary.analyzed == 2
    assert evidence.partial is False


def test_analysis_maps_acquisition_failures_without_calling_analyzers() -> None:
    downloads = ArticleMediaDownloads(
        items=(
            MediaAcquisitionItem(
                index=0,
                source_url="https://example.test/blocked",
                status=MediaAcquisitionStatus.SKIPPED,
                reason=MediaItemReason.BLOCKED_HOST,
            ),
            MediaAcquisitionItem(
                index=1,
                source_url="https://mmbiz.qpic.cn/failed",
                status=MediaAcquisitionStatus.FAILED,
                reason=MediaItemReason.DOWNLOAD_FAILED,
            ),
        ),
        total_bytes=0,
    )

    evidence, qr, ocr = analyze(downloads)

    assert qr.calls == []
    assert ocr.calls == []
    assert [item.status for item in evidence.items] == [
        MediaItemStatus.SKIPPED,
        MediaItemStatus.FAILED,
    ]
    assert evidence.partial is True


def test_qr_failure_is_isolated_from_ocr_and_uses_stable_guard() -> None:
    evidence, _, ocr = analyze(
        acquired(downloaded(b"image")),
        qr=FakeQRAnalyzer(fail=True),
    )

    item = evidence.items[0]
    assert item.qr is not None and item.qr.status == QRStatus.FAILED
    assert item.qr.analyzer == ANALYSIS_GUARD
    assert item.ocr is not None and item.ocr.status == OCRStatus.ANALYZED
    assert len(ocr.calls) == 1
    assert evidence.partial is True


def test_ocr_failure_is_isolated_from_qr_and_uses_stable_guard() -> None:
    evidence, qr, _ = analyze(
        acquired(downloaded(b"image")),
        ocr=FakeOCRProvider(fail=True),
    )

    item = evidence.items[0]
    assert item.qr is not None and item.qr.status == QRStatus.NOT_FOUND
    assert item.ocr is not None and item.ocr.status == OCRStatus.FAILED
    assert item.ocr.analyzer == ANALYSIS_GUARD
    assert len(qr.calls) == 1


def test_mismatched_analyzer_links_are_replaced_by_failed_guard_evidence() -> None:
    evidence, _, _ = analyze(
        acquired(downloaded(b"image")),
        qr=FakeQRAnalyzer(wrong_hash=True),
        ocr=FakeOCRProvider(wrong_language=True),
    )

    assert evidence.items[0].qr is not None
    assert evidence.items[0].qr.status == QRStatus.FAILED
    assert evidence.items[0].ocr is not None
    assert evidence.items[0].ocr.status == OCRStatus.FAILED


def test_invalid_downloaded_metadata_is_failed_before_analysis() -> None:
    media = downloaded(b"image")
    invalid = DownloadedMedia(
        source_url=media.source_url,
        final_url=media.final_url,
        content=media.content,
        byte_sha256="f" * 64,
        media_format=media.media_format,
        media_type=media.media_type,
        byte_length=media.byte_length,
        width=media.width,
        height=media.height,
        redirect_urls=media.redirect_urls,
    )

    evidence, qr, ocr = analyze(acquired(invalid))

    assert qr.calls == []
    assert ocr.calls == []
    assert evidence.items[0].status == MediaItemStatus.FAILED
    assert evidence.items[0].reason == MediaItemReason.MALFORMED_IMAGE


def test_batch_ocr_character_limit_bounds_emitted_occurrence_text() -> None:
    configuration = MediaAnalysisConfiguration(
        limits=MediaAnalysisLimits(max_ocr_characters_per_batch=5)
    )
    evidence, _, _ = analyze(
        acquired(downloaded(b"one"), downloaded(b"two")),
        ocr=FakeOCRProvider(text="abcd"),
        configuration=configuration,
    )

    assert evidence.items[0].ocr is not None
    assert evidence.items[0].ocr.text == "abcd"
    assert evidence.items[0].ocr.truncated is False
    assert evidence.items[1].ocr is not None
    assert evidence.items[1].ocr.text == "a"
    assert evidence.items[1].ocr.truncated is True


def test_lowered_qr_and_per_image_ocr_limits_are_enforced_by_orchestrator() -> None:
    configuration = MediaAnalysisConfiguration(
        limits=MediaAnalysisLimits(
            max_qr_payloads_per_image=1,
            max_qr_payload_bytes=3,
            max_ocr_characters_per_image=2,
        )
    )
    qr = PayloadQRAnalyzer()

    evidence, _, _ = analyze(
        acquired(downloaded(b"image")),
        qr=qr,
        ocr=FakeOCRProvider(text="abcd"),
        configuration=configuration,
    )

    assert evidence.items[0].qr is not None
    assert evidence.items[0].qr.status == QRStatus.FAILED
    assert evidence.items[0].ocr is not None
    assert evidence.items[0].ocr.text == "ab"
    assert evidence.items[0].ocr.truncated is True


def test_lowered_media_limits_are_rechecked_before_analysis() -> None:
    configuration = MediaAnalysisConfiguration(
        limits=MediaAnalysisLimits(max_image_bytes=4, max_image_pixels=50)
    )
    evidence, qr, ocr = analyze(
        acquired(downloaded(b"image")),
        configuration=configuration,
    )

    assert evidence.items[0].status == MediaItemStatus.FAILED
    assert evidence.items[0].reason == MediaItemReason.TOO_LARGE
    assert qr.calls == []
    assert ocr.calls == []


def test_download_batch_must_match_recorded_article_limits() -> None:
    downloads = acquired(downloaded(b"one"), downloaded(b"two"))
    configuration = MediaAnalysisConfiguration(
        limits=MediaAnalysisLimits(max_article_images=1)
    )

    with pytest.raises(ValueError, match="Article image limit"):
        analyze(downloads, configuration=configuration)


def test_omitted_downloads_remain_visible_and_make_evidence_partial() -> None:
    evidence, _, _ = analyze(
        acquired(downloaded(b"image"), omitted_count=4)
    )

    assert evidence.omitted_count == 4
    assert evidence.summary.omitted == 4
    assert evidence.partial is True
