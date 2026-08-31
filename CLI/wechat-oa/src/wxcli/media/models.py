"""Strict, versioned models for derived image, QR, and OCR evidence."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import datetime
from enum import StrEnum
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from wxcli.evidence import ArticleEvidence

MEDIA_EVIDENCE_SCHEMA_VERSION: Literal["1"] = "1"
MEDIA_EVIDENCE_EXTRACTOR_VERSION = "1"
MEDIA_RESULT_SCHEMA_VERSION: Literal["2"] = "2"
OCR_NORMALIZATION_VERSION: Literal["1"] = "1"

SHA256_PATTERN = r"^[0-9a-f]{64}$"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_ARTICLE_IMAGES = 50
MAX_ARTICLE_BYTES = 100 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_QR_PAYLOADS_PER_IMAGE = 20
MAX_QR_PAYLOAD_BYTES = 4 * 1024
MAX_OCR_CHARACTERS_PER_IMAGE = 50_000


class MediaItemStatus(StrEnum):
    """Stable outcome categories for one Article image occurrence."""

    ANALYZED = "analyzed"
    SKIPPED = "skipped"
    FAILED = "failed"


class MediaItemReason(StrEnum):
    """Why an image occurrence was skipped or failed before local analysis."""

    BLOCKED_HOST = "blocked_host"
    UNSAFE_DESTINATION = "unsafe_destination"
    DOWNLOAD_FAILED = "download_failed"
    DOWNLOAD_TIMEOUT = "download_timeout"
    DOWNLOAD_FORBIDDEN = "download_forbidden"
    TOO_LARGE = "too_large"
    RESOURCE_LIMIT = "resource_limit"
    UNSUPPORTED_FORMAT = "unsupported_format"
    MALFORMED_IMAGE = "malformed_image"
    PIXEL_LIMIT = "pixel_limit"


class MediaFormat(StrEnum):
    JPEG = "jpeg"
    PNG = "png"
    WEBP = "webp"
    GIF = "gif"


class QRStatus(StrEnum):
    DECODED = "decoded"
    NOT_FOUND = "not_found"
    FAILED = "failed"


class QRPayloadType(StrEnum):
    URL = "url"
    TEXT = "text"
    CONTACT = "contact"
    UNKNOWN = "unknown"


class OCRStatus(StrEnum):
    ANALYZED = "analyzed"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class MediaAnalysisLimits(BaseModel):
    """Result-affecting hard limits recorded with every Media Evidence document."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_image_bytes: int = Field(default=MAX_IMAGE_BYTES, ge=1, le=MAX_IMAGE_BYTES)
    max_article_images: int = Field(default=MAX_ARTICLE_IMAGES, ge=1, le=MAX_ARTICLE_IMAGES)
    max_article_bytes: int = Field(
        default=MAX_ARTICLE_BYTES,
        ge=1,
        le=MAX_ARTICLE_BYTES,
    )
    max_image_pixels: int = Field(default=MAX_IMAGE_PIXELS, ge=1, le=MAX_IMAGE_PIXELS)
    max_qr_payloads_per_image: int = Field(
        default=MAX_QR_PAYLOADS_PER_IMAGE,
        ge=1,
        le=MAX_QR_PAYLOADS_PER_IMAGE,
    )
    max_qr_payload_bytes: int = Field(
        default=MAX_QR_PAYLOAD_BYTES,
        ge=1,
        le=MAX_QR_PAYLOAD_BYTES,
    )
    max_ocr_characters_per_image: int = Field(
        default=MAX_OCR_CHARACTERS_PER_IMAGE,
        ge=1,
        le=MAX_OCR_CHARACTERS_PER_IMAGE,
    )


class MediaAnalysisConfiguration(BaseModel):
    """Trusted, invocation-owned settings that affect derived Media Evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ocr_language: str = Field(default="zh-Hans", min_length=1, max_length=64)
    limits: MediaAnalysisLimits = Field(default_factory=MediaAnalysisLimits)


class QRPayloadEvidence(BaseModel):
    """One inert QR payload in stable analyzer order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0)
    payload_type: QRPayloadType
    payload: str = Field(max_length=MAX_QR_PAYLOAD_BYTES)
    payload_sha256: str = Field(pattern=SHA256_PATTERN)

    @classmethod
    def from_payload(
        cls,
        *,
        index: int,
        payload_type: QRPayloadType,
        payload: str,
    ) -> QRPayloadEvidence:
        return cls(
            index=index,
            payload_type=payload_type,
            payload=payload,
            payload_sha256=_text_sha256(payload),
        )

    @model_validator(mode="after")
    def validate_payload(self) -> QRPayloadEvidence:
        if len(self.payload.encode("utf-8")) > MAX_QR_PAYLOAD_BYTES:
            raise ValueError("QR payloads are limited to 4 KiB of UTF-8 data.")
        if self.payload_sha256 != _text_sha256(self.payload):
            raise ValueError("QR payload_sha256 does not match payload.")
        return self


class QREvidence(BaseModel):
    """Local standard-QR analyzer outcome for one decoded image."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_byte_sha256: str = Field(pattern=SHA256_PATTERN)
    analyzer: str = Field(min_length=1, max_length=100)
    analyzer_version: str = Field(min_length=1, max_length=100)
    status: QRStatus
    payloads: tuple[QRPayloadEvidence, ...] = Field(
        default_factory=tuple,
        max_length=MAX_QR_PAYLOADS_PER_IMAGE,
    )

    @model_validator(mode="after")
    def validate_outcome(self) -> QREvidence:
        expected_indexes = list(range(len(self.payloads)))
        if [payload.index for payload in self.payloads] != expected_indexes:
            raise ValueError("QR payload indexes must be contiguous and ordered from zero.")
        if self.status == QRStatus.DECODED and not self.payloads:
            raise ValueError("Decoded QR Evidence requires at least one payload.")
        if self.status != QRStatus.DECODED and self.payloads:
            raise ValueError("Only decoded QR Evidence may contain payloads.")
        return self


class OCREvidence(BaseModel):
    """Normalized local OCR outcome for one decoded image."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_byte_sha256: str = Field(pattern=SHA256_PATTERN)
    analyzer: str = Field(min_length=1, max_length=100)
    analyzer_version: str = Field(min_length=1, max_length=100)
    normalization_version: Literal["1"] = OCR_NORMALIZATION_VERSION
    status: OCRStatus
    requested_language: str = Field(default="zh-Hans", min_length=1, max_length=64)
    detected_language: str | None = Field(default=None, min_length=1, max_length=64)
    confidence: float | None = Field(default=None, ge=0, le=1)
    text: str | None = None
    truncated: bool = False

    @field_validator("text", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
        return "".join(
            character
            for character in normalized
            if character in {"\n", "\t"} or unicodedata.category(character) != "Cc"
        )

    @model_validator(mode="after")
    def validate_outcome(self) -> OCREvidence:
        if self.status == OCRStatus.ANALYZED:
            if self.text is None:
                raise ValueError("Analyzed OCR Evidence requires text, including an empty string.")
            if len(self.text) > MAX_OCR_CHARACTERS_PER_IMAGE:
                raise ValueError("OCR text exceeds the per-image character limit.")
            return self
        if self.text is not None or self.confidence is not None or self.truncated:
            raise ValueError("Unavailable or failed OCR Evidence cannot contain derived text.")
        return self


class MediaItemEvidence(BaseModel):
    """Download and analysis outcome for one Article image occurrence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0)
    source_url: str = Field(min_length=1, max_length=4096)
    status: MediaItemStatus
    reason: MediaItemReason | None = None
    cache_hit: bool = False
    byte_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    media_format: MediaFormat | None = None
    media_type: str | None = Field(default=None, max_length=100)
    byte_length: int | None = Field(default=None, ge=1, le=MAX_IMAGE_BYTES)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    qr: QREvidence | None = None
    ocr: OCREvidence | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> MediaItemEvidence:
        binary_fields = (
            self.byte_sha256,
            self.media_format,
            self.media_type,
            self.byte_length,
            self.width,
            self.height,
        )
        if self.status == MediaItemStatus.SKIPPED:
            if self.reason is None:
                raise ValueError("Skipped Media Item Evidence requires a reason.")
            if any(value is not None for value in binary_fields) or self.qr or self.ocr:
                raise ValueError("Skipped Media Item Evidence cannot contain downloaded data.")
            if self.cache_hit:
                raise ValueError("Skipped Media Item Evidence cannot be a cache hit.")
            return self

        if self.status == MediaItemStatus.FAILED:
            if self.reason is None:
                raise ValueError("Failed Media Item Evidence requires a reason.")
            if self.qr is not None or self.ocr is not None:
                raise ValueError("Failed Media Item Evidence cannot contain derived analysis.")
            return self

        if self.reason is not None:
            raise ValueError("Analyzed Media Item Evidence cannot contain a skip/failure reason.")
        if any(value is None for value in binary_fields) or self.qr is None or self.ocr is None:
            raise ValueError("Analyzed Media Item Evidence requires complete image, QR, and OCR data.")
        assert self.width is not None
        assert self.height is not None
        assert self.media_format is not None
        if self.width * self.height > MAX_IMAGE_PIXELS:
            raise ValueError("Decoded image exceeds the pixel limit.")
        expected_media_type = {
            MediaFormat.JPEG: "image/jpeg",
            MediaFormat.PNG: "image/png",
            MediaFormat.WEBP: "image/webp",
            MediaFormat.GIF: "image/gif",
        }[self.media_format]
        if self.media_type != expected_media_type:
            raise ValueError("Detected image format and media type do not agree.")
        if self.qr.source_byte_sha256 != self.byte_sha256:
            raise ValueError("QR Evidence is linked to different image bytes.")
        if self.ocr.source_byte_sha256 != self.byte_sha256:
            raise ValueError("OCR Evidence is linked to different image bytes.")
        return self


class MediaSummary(BaseModel):
    """Deterministic counts derived from Media Item Evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total: int = Field(ge=0)
    analyzed: int = Field(ge=0)
    skipped: int = Field(ge=0)
    failed: int = Field(ge=0)
    qr_decoded: int = Field(ge=0)
    qr_not_found: int = Field(ge=0)
    qr_failed: int = Field(ge=0)
    ocr_analyzed: int = Field(ge=0)
    ocr_unavailable: int = Field(ge=0)
    ocr_failed: int = Field(ge=0)


class MediaEvidence(BaseModel):
    """Separately versioned derived evidence linked to Article Evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = MEDIA_EVIDENCE_SCHEMA_VERSION
    extractor_version: str = MEDIA_EVIDENCE_EXTRACTOR_VERSION
    source_content_sha256: str = Field(pattern=SHA256_PATTERN)
    configuration: MediaAnalysisConfiguration
    analysis_started_at: datetime
    analysis_finished_at: datetime
    partial: bool
    summary: MediaSummary
    items: tuple[MediaItemEvidence, ...] = Field(max_length=MAX_ARTICLE_IMAGES)
    media_evidence_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_document(self) -> MediaEvidence:
        if not _is_aware(self.analysis_started_at) or not _is_aware(self.analysis_finished_at):
            raise ValueError("Media Evidence timestamps must be timezone-aware.")
        if self.analysis_finished_at < self.analysis_started_at:
            raise ValueError("Media Evidence cannot finish before it starts.")
        if [item.index for item in self.items] != list(range(len(self.items))):
            raise ValueError("Media item indexes must be contiguous and ordered from zero.")
        if self.summary != _summarize(self.items):
            raise ValueError("Media summary does not match item outcomes.")
        if self.partial != _is_partial(self.items):
            raise ValueError("Media partial flag does not match item outcomes.")
        if self.media_evidence_sha256 != _media_evidence_hash(self):
            raise ValueError("Media Evidence hash does not match its stable content.")
        return self


class MediaAnalysisResult(BaseModel):
    """Schema-v2 outer result for explicitly media-enabled commands."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["2"] = MEDIA_RESULT_SCHEMA_VERSION
    article_evidence: ArticleEvidence
    media_evidence: MediaEvidence

    @model_validator(mode="after")
    def validate_link(self) -> MediaAnalysisResult:
        if self.media_evidence.source_content_sha256 != self.article_evidence.content_sha256:
            raise ValueError("Media Evidence must link to the embedded Article Evidence.")
        return self


def build_media_evidence(
    *,
    source_content_sha256: str,
    items: Sequence[MediaItemEvidence],
    analysis_started_at: datetime,
    analysis_finished_at: datetime,
    configuration: MediaAnalysisConfiguration | None = None,
) -> MediaEvidence:
    """Build a validated Media Evidence document and its stable fingerprint."""
    actual_configuration = configuration or MediaAnalysisConfiguration()
    immutable_items = tuple(items)
    summary = _summarize(immutable_items)
    partial = _is_partial(immutable_items)
    payload = {
        "schema_version": MEDIA_EVIDENCE_SCHEMA_VERSION,
        "extractor_version": MEDIA_EVIDENCE_EXTRACTOR_VERSION,
        "source_content_sha256": source_content_sha256,
        "configuration": actual_configuration.model_dump(mode="json"),
        "partial": partial,
        "summary": summary.model_dump(mode="json"),
        "items": [_stable_item_payload(item) for item in immutable_items],
    }
    return MediaEvidence(
        source_content_sha256=source_content_sha256,
        configuration=actual_configuration,
        analysis_started_at=analysis_started_at,
        analysis_finished_at=analysis_finished_at,
        partial=partial,
        summary=summary,
        items=immutable_items,
        media_evidence_sha256=_json_sha256(payload),
    )


def _summarize(items: Sequence[MediaItemEvidence]) -> MediaSummary:
    qr_statuses = [item.qr.status for item in items if item.qr is not None]
    ocr_statuses = [item.ocr.status for item in items if item.ocr is not None]
    return MediaSummary(
        total=len(items),
        analyzed=sum(item.status == MediaItemStatus.ANALYZED for item in items),
        skipped=sum(item.status == MediaItemStatus.SKIPPED for item in items),
        failed=sum(item.status == MediaItemStatus.FAILED for item in items),
        qr_decoded=qr_statuses.count(QRStatus.DECODED),
        qr_not_found=qr_statuses.count(QRStatus.NOT_FOUND),
        qr_failed=qr_statuses.count(QRStatus.FAILED),
        ocr_analyzed=ocr_statuses.count(OCRStatus.ANALYZED),
        ocr_unavailable=ocr_statuses.count(OCRStatus.UNAVAILABLE),
        ocr_failed=ocr_statuses.count(OCRStatus.FAILED),
    )


def _is_partial(items: Sequence[MediaItemEvidence]) -> bool:
    return any(
        item.status != MediaItemStatus.ANALYZED
        or (item.qr is not None and item.qr.status == QRStatus.FAILED)
        or (item.ocr is not None and item.ocr.status != OCRStatus.ANALYZED)
        for item in items
    )


def _stable_item_payload(item: MediaItemEvidence) -> dict[str, object]:
    return item.model_dump(mode="json", exclude={"cache_hit"})


def _media_evidence_hash(evidence: MediaEvidence) -> str:
    return _json_sha256(
        {
            "schema_version": evidence.schema_version,
            "extractor_version": evidence.extractor_version,
            "source_content_sha256": evidence.source_content_sha256,
            "configuration": evidence.configuration.model_dump(mode="json"),
            "partial": evidence.partial,
            "summary": evidence.summary.model_dump(mode="json"),
            "items": [_stable_item_payload(item) for item in evidence.items],
        }
    )


def _json_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None
