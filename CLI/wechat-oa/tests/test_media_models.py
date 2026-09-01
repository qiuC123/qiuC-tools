from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from wxcli.evidence import (
    AccountIdentityEvidence,
    ArticleEvidence,
    IdentityStatus,
)
from wxcli.media import (
    MediaAnalysisConfiguration,
    MediaAnalysisLimits,
    MediaAnalysisResult,
    MediaFormat,
    MediaItemEvidence,
    MediaItemReason,
    MediaItemStatus,
    OCREvidence,
    OCRStatus,
    QREvidence,
    QRPayloadEvidence,
    QRPayloadType,
    QRStatus,
    build_media_evidence,
)
from wxcli.models import Article, Provider


def digest(character: str) -> str:
    return character * 64


def qr_not_found(byte_sha256: str) -> QREvidence:
    return QREvidence(
        source_byte_sha256=byte_sha256,
        analyzer="standard-qr",
        analyzer_version="1.0",
        status=QRStatus.NOT_FOUND,
    )


def ocr_analyzed(byte_sha256: str, text: str = "校园招聘") -> OCREvidence:
    return OCREvidence(
        source_byte_sha256=byte_sha256,
        analyzer="windows-ocr",
        analyzer_version="1.0",
        status=OCRStatus.ANALYZED,
        text=text,
        confidence=0.9,
    )


def analyzed_item(
    *,
    index: int = 0,
    byte_sha256: str | None = None,
    cache_hit: bool = False,
    ocr: OCREvidence | None = None,
) -> MediaItemEvidence:
    actual_hash = byte_sha256 or digest("a")
    return MediaItemEvidence(
        index=index,
        source_url=f"https://mmbiz.qpic.cn/example/{index}",
        status=MediaItemStatus.ANALYZED,
        cache_hit=cache_hit,
        byte_sha256=actual_hash,
        media_format=MediaFormat.PNG,
        media_type="image/png",
        byte_length=1024,
        width=100,
        height=200,
        qr=qr_not_found(actual_hash),
        ocr=ocr or ocr_analyzed(actual_hash),
    )


def test_media_evidence_builds_stable_versioned_document() -> None:
    started = datetime(2026, 1, 1, tzinfo=UTC)
    finished = datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)

    evidence = build_media_evidence(
        source_content_sha256=digest("c"),
        items=[analyzed_item()],
        analysis_started_at=started,
        analysis_finished_at=finished,
    )

    assert evidence.schema_version == "1"
    assert evidence.source_content_sha256 == digest("c")
    assert evidence.partial is False
    assert evidence.summary.model_dump() == {
        "total": 1,
        "omitted": 0,
        "analyzed": 1,
        "skipped": 0,
        "failed": 0,
        "qr_decoded": 0,
        "qr_not_found": 1,
        "qr_failed": 0,
        "ocr_analyzed": 1,
        "ocr_unavailable": 0,
        "ocr_failed": 0,
    }
    assert len(evidence.media_evidence_sha256) == 64


def test_omitted_occurrences_make_media_evidence_partial_and_affect_hash() -> None:
    arguments = {
        "source_content_sha256": digest("c"),
        "items": [analyzed_item()],
        "analysis_started_at": datetime(2026, 1, 1, tzinfo=UTC),
        "analysis_finished_at": datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
    }

    complete = build_media_evidence(**arguments)
    omitted = build_media_evidence(**arguments, omitted_count=3)

    assert omitted.partial is True
    assert omitted.omitted_count == 3
    assert omitted.summary.omitted == 3
    assert omitted.media_evidence_sha256 != complete.media_evidence_sha256


def test_media_limits_may_be_lowered_but_never_exceed_hard_caps() -> None:
    configuration = MediaAnalysisConfiguration(
        limits=MediaAnalysisLimits(
            max_image_bytes=1024,
            max_ocr_characters_per_batch=1234,
        )
    )

    assert configuration.limits.max_image_bytes == 1024
    assert configuration.limits.max_ocr_characters_per_batch == 1234
    with pytest.raises(ValidationError):
        MediaAnalysisLimits(max_image_bytes=10 * 1024 * 1024 + 1)


def test_media_hash_ignores_run_times_and_cache_hits() -> None:
    first = build_media_evidence(
        source_content_sha256=digest("c"),
        items=[analyzed_item(cache_hit=False)],
        analysis_started_at=datetime(2026, 1, 1, tzinfo=UTC),
        analysis_finished_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
    )
    second = build_media_evidence(
        source_content_sha256=digest("c"),
        items=[analyzed_item(cache_hit=True)],
        analysis_started_at=datetime(2026, 2, 1, tzinfo=UTC),
        analysis_finished_at=datetime(2026, 2, 1, 0, 0, 2, tzinfo=UTC),
    )

    assert first.analysis_started_at != second.analysis_started_at
    assert first.items[0].cache_hit is False
    assert second.items[0].cache_hit is True
    assert first.media_evidence_sha256 == second.media_evidence_sha256


def test_media_item_collections_are_copied_into_immutable_tuples() -> None:
    items = [analyzed_item()]
    evidence = build_media_evidence(
        source_content_sha256=digest("c"),
        items=items,
        analysis_started_at=datetime(2026, 1, 1, tzinfo=UTC),
        analysis_finished_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
    )

    items.clear()

    assert isinstance(evidence.items, tuple)
    assert len(evidence.items) == 1
    assert isinstance(evidence.items[0].qr.payloads, tuple)


def test_media_hash_changes_when_derived_text_changes() -> None:
    first = build_media_evidence(
        source_content_sha256=digest("c"),
        items=[analyzed_item()],
        analysis_started_at=datetime(2026, 1, 1, tzinfo=UTC),
        analysis_finished_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
    )
    changed_ocr = ocr_analyzed(digest("a"), text="社会招聘")
    second = build_media_evidence(
        source_content_sha256=digest("c"),
        items=[analyzed_item(ocr=changed_ocr)],
        analysis_started_at=datetime(2026, 1, 1, tzinfo=UTC),
        analysis_finished_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
    )

    assert first.media_evidence_sha256 != second.media_evidence_sha256


def test_partial_tracks_skips_failures_and_optional_ocr_capability() -> None:
    unavailable_ocr = OCREvidence(
        source_byte_sha256=digest("a"),
        analyzer="windows-ocr",
        analyzer_version="1.0",
        status=OCRStatus.UNAVAILABLE,
    )
    skipped = MediaItemEvidence(
        index=1,
        source_url="https://example.com/not-allowed.png",
        status=MediaItemStatus.SKIPPED,
        reason=MediaItemReason.BLOCKED_HOST,
    )

    evidence = build_media_evidence(
        source_content_sha256=digest("c"),
        items=[analyzed_item(ocr=unavailable_ocr), skipped],
        analysis_started_at=datetime(2026, 1, 1, tzinfo=UTC),
        analysis_finished_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
    )

    assert evidence.partial is True
    assert evidence.summary.analyzed == 1
    assert evidence.summary.skipped == 1
    assert evidence.summary.ocr_unavailable == 1


def test_qr_payload_is_bounded_ordered_and_hash_verified() -> None:
    payload = QRPayloadEvidence.from_payload(
        index=0,
        payload_type=QRPayloadType.URL,
        payload="https://example.com/apply",
    )
    evidence = QREvidence(
        source_byte_sha256=digest("a"),
        analyzer="standard-qr",
        analyzer_version="1.0",
        status=QRStatus.DECODED,
        payloads=[payload],
    )

    assert evidence.payloads[0].payload_sha256 != evidence.source_byte_sha256
    with pytest.raises(ValidationError, match="payload_sha256"):
        QRPayloadEvidence(
            index=0,
            payload_type=QRPayloadType.TEXT,
            payload="safe inert text",
            payload_sha256=digest("f"),
        )
    with pytest.raises(ValidationError, match="indexes"):
        QREvidence(
            source_byte_sha256=digest("a"),
            analyzer="standard-qr",
            analyzer_version="1.0",
            status=QRStatus.DECODED,
            payloads=[payload.model_copy(update={"index": 1})],
        )


def test_ocr_normalizes_text_and_rejects_derived_data_on_failure() -> None:
    evidence = ocr_analyzed(digest("a"), text="A\r\nB\x1b[31m")

    assert evidence.text == "A\nB[31m"
    with pytest.raises(ValidationError, match="cannot contain derived text"):
        OCREvidence(
            source_byte_sha256=digest("a"),
            analyzer="windows-ocr",
            analyzer_version="1.0",
            status=OCRStatus.FAILED,
            text="invented",
        )
    with pytest.raises(ValidationError, match="cannot contain derived text"):
        OCREvidence(
            source_byte_sha256=digest("a"),
            analyzer="windows-ocr",
            analyzer_version="1.0",
            status=OCRStatus.UNAVAILABLE,
            preprocessing=("grayscale",),
        )


def test_item_outcomes_reject_invented_or_mismatched_analysis() -> None:
    with pytest.raises(ValidationError, match="cannot contain downloaded data"):
        MediaItemEvidence(
            index=0,
            source_url="https://example.com/blocked.png",
            status=MediaItemStatus.SKIPPED,
            reason=MediaItemReason.BLOCKED_HOST,
            byte_sha256=digest("a"),
        )
    with pytest.raises(ValidationError, match="different image bytes"):
        MediaItemEvidence(
            index=0,
            source_url="https://mmbiz.qpic.cn/example/0",
            status=MediaItemStatus.ANALYZED,
            byte_sha256=digest("a"),
            media_format=MediaFormat.PNG,
            media_type="image/png",
            byte_length=1024,
            width=100,
            height=200,
            qr=qr_not_found(digest("b")),
            ocr=ocr_analyzed(digest("a")),
        )


def test_media_result_links_to_unchanged_article_evidence_v1() -> None:
    article_evidence = ArticleEvidence(
        article=Article(
            title="Local fixture",
            content_markdown="body",
            provider=Provider.LOCAL,
        ),
        account_identity=AccountIdentityEvidence(status=IdentityStatus.UNKNOWN),
        last_verified_at=datetime(2026, 1, 1, tzinfo=UTC),
        content_sha256=digest("c"),
        evidence_sha256=digest("e"),
    )
    media_evidence = build_media_evidence(
        source_content_sha256=digest("c"),
        items=[],
        analysis_started_at=datetime(2026, 1, 1, tzinfo=UTC),
        analysis_finished_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    result = MediaAnalysisResult(
        article_evidence=article_evidence,
        media_evidence=media_evidence,
    )

    assert result.schema_version == "2"
    assert result.article_evidence.schema_version == "1"
    assert "media_evidence" not in result.article_evidence.model_dump()
    with pytest.raises(ValidationError, match="must link"):
        MediaAnalysisResult(
            article_evidence=article_evidence.model_copy(
                update={"content_sha256": digest("d")}
            ),
            media_evidence=media_evidence,
        )


def test_media_document_rejects_naive_or_reversed_timestamps() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        build_media_evidence(
            source_content_sha256=digest("c"),
            items=[],
            analysis_started_at=datetime(2026, 1, 1),
            analysis_finished_at=datetime(2026, 1, 1),
        )
    with pytest.raises(ValidationError, match="finish before"):
        build_media_evidence(
            source_content_sha256=digest("c"),
            items=[],
            analysis_started_at=datetime(2026, 1, 2, tzinfo=UTC),
            analysis_finished_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
