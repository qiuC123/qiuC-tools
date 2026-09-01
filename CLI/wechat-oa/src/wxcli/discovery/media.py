"""Bounded media analysis for verified candidates in one discovery result."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from wxcli.discovery.models import CandidateIngestionResult, DiscoveryResult
from wxcli.evidence import ArticleEvidence
from wxcli.media.models import (
    MAX_ARTICLE_BYTES,
    MAX_ARTICLE_IMAGES,
    MAX_OCR_CHARACTERS_PER_BATCH,
    MediaEvidence,
)

DISCOVERY_MEDIA_RESULT_SCHEMA_VERSION: Literal["2"] = "2"
MAX_DISCOVERY_MEDIA_IMAGES = 200
MAX_DISCOVERY_MEDIA_BYTES = 400 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class DiscoveryMediaBudget:
    """Remaining trusted limits passed to one Article media analysis."""

    max_images: int
    max_bytes: int
    max_ocr_characters: int

    def __post_init__(self) -> None:
        if not 1 <= self.max_images <= MAX_ARTICLE_IMAGES:
            raise ValueError("Discovery Article image budget is invalid.")
        if not 1 <= self.max_bytes <= MAX_ARTICLE_BYTES:
            raise ValueError("Discovery Article byte budget is invalid.")
        if not 1 <= self.max_ocr_characters <= MAX_OCR_CHARACTERS_PER_BATCH:
            raise ValueError("Discovery Article OCR budget is invalid.")


class DiscoveryMediaBatchLimits(BaseModel):
    """Invocation-owned hard limits recorded in a media-enabled result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_images: int = Field(
        default=MAX_DISCOVERY_MEDIA_IMAGES,
        ge=1,
        le=MAX_DISCOVERY_MEDIA_IMAGES,
    )
    max_bytes: int = Field(
        default=MAX_DISCOVERY_MEDIA_BYTES,
        ge=1,
        le=MAX_DISCOVERY_MEDIA_BYTES,
    )
    max_ocr_characters: int = Field(
        default=MAX_OCR_CHARACTERS_PER_BATCH,
        ge=1,
        le=MAX_OCR_CHARACTERS_PER_BATCH,
    )


class CandidateMediaEvidence(BaseModel):
    """One Media Evidence document linked to its discovery candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_index: int = Field(ge=0)
    article_identity: str = Field(min_length=1, max_length=512)
    media_evidence: MediaEvidence


class DiscoveryMediaSummary(BaseModel):
    """Deterministic batch observations for explicitly enabled media analysis."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    eligible_articles: int = Field(ge=0)
    analyzed_articles: int = Field(ge=0)
    omitted_articles: int = Field(ge=0)
    image_items: int = Field(ge=0)
    omitted_images: int = Field(ge=0)
    downloaded_bytes: int = Field(ge=0)
    ocr_characters: int = Field(ge=0)
    partial: bool


class DiscoveryMediaAnalysisResult(BaseModel):
    """Schema-v2 wrapper that preserves the complete schema-v1 discovery result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["2"] = DISCOVERY_MEDIA_RESULT_SCHEMA_VERSION
    discovery_result: DiscoveryResult | CandidateIngestionResult
    limits: DiscoveryMediaBatchLimits
    summary: DiscoveryMediaSummary
    media: tuple[CandidateMediaEvidence, ...]

    @model_validator(mode="after")
    def validate_links_and_summary(self) -> DiscoveryMediaAnalysisResult:
        candidates = self.discovery_result.candidates
        if [item.candidate_index for item in self.media] != sorted(
            item.candidate_index for item in self.media
        ):
            raise ValueError("Discovery Media Evidence must preserve candidate order.")
        if len({item.candidate_index for item in self.media}) != len(self.media):
            raise ValueError("A discovery candidate cannot have duplicate Media Evidence.")
        for item in self.media:
            if item.candidate_index >= len(candidates):
                raise ValueError("Discovery Media Evidence references a missing candidate.")
            candidate = candidates[item.candidate_index]
            if candidate.article_identity != item.article_identity:
                raise ValueError("Discovery Media Evidence has the wrong article identity.")
            if (
                candidate.evidence is None
                or candidate.evidence.content_sha256
                != item.media_evidence.source_content_sha256
            ):
                raise ValueError("Discovery Media Evidence is linked to different Article Evidence.")
        if self.summary != _summarize(
            self.discovery_result,
            self.media,
        ):
            raise ValueError("Discovery media summary does not match its evidence.")
        if self.summary.image_items > self.limits.max_images:
            raise ValueError("Discovery media result exceeds the image limit.")
        if self.summary.downloaded_bytes > self.limits.max_bytes:
            raise ValueError("Discovery media result exceeds the byte limit.")
        if self.summary.ocr_characters > self.limits.max_ocr_characters:
            raise ValueError("Discovery media result exceeds the OCR character limit.")
        return self


class DiscoveryMediaAnalyzer:
    """Analyze verified candidates serially under one deterministic batch budget."""

    def __init__(
        self,
        analyze_article: Callable[[ArticleEvidence, DiscoveryMediaBudget], MediaEvidence],
        *,
        limits: DiscoveryMediaBatchLimits | None = None,
    ) -> None:
        self._analyze_article = analyze_article
        self._limits = limits or DiscoveryMediaBatchLimits()

    def analyze(
        self,
        discovery_result: DiscoveryResult | CandidateIngestionResult,
    ) -> DiscoveryMediaAnalysisResult:
        remaining_images = self._limits.max_images
        remaining_bytes = self._limits.max_bytes
        remaining_ocr = self._limits.max_ocr_characters
        media: list[CandidateMediaEvidence] = []

        for candidate_index, candidate in enumerate(discovery_result.candidates):
            evidence = candidate.evidence
            if evidence is None:
                continue
            if remaining_images <= 0 or remaining_bytes <= 0 or remaining_ocr <= 0:
                break
            budget = DiscoveryMediaBudget(
                max_images=min(MAX_ARTICLE_IMAGES, remaining_images),
                max_bytes=min(MAX_ARTICLE_BYTES, remaining_bytes),
                max_ocr_characters=min(
                    MAX_OCR_CHARACTERS_PER_BATCH,
                    remaining_ocr,
                ),
            )
            analyzed = self._analyze_article(evidence, budget)
            if analyzed.source_content_sha256 != evidence.content_sha256:
                raise ValueError("Media Evidence is linked to different Article Evidence.")
            image_items = len(analyzed.items)
            downloaded_bytes = _downloaded_bytes(analyzed)
            ocr_characters = _ocr_characters(analyzed)
            if (
                image_items > budget.max_images
                or downloaded_bytes > budget.max_bytes
                or ocr_characters > budget.max_ocr_characters
            ):
                raise ValueError("Article media analysis exceeded its discovery budget.")
            remaining_images -= image_items
            remaining_bytes -= downloaded_bytes
            remaining_ocr -= ocr_characters
            media.append(
                CandidateMediaEvidence(
                    candidate_index=candidate_index,
                    article_identity=candidate.article_identity,
                    media_evidence=analyzed,
                )
            )

        immutable_media = tuple(media)
        return DiscoveryMediaAnalysisResult(
            discovery_result=discovery_result,
            limits=self._limits,
            summary=_summarize(discovery_result, immutable_media),
            media=immutable_media,
        )


def _summarize(
    discovery_result: DiscoveryResult | CandidateIngestionResult,
    media: tuple[CandidateMediaEvidence, ...],
) -> DiscoveryMediaSummary:
    eligible = sum(candidate.evidence is not None for candidate in discovery_result.candidates)
    analyzed = len(media)
    omitted = eligible - analyzed
    return DiscoveryMediaSummary(
        eligible_articles=eligible,
        analyzed_articles=analyzed,
        omitted_articles=omitted,
        image_items=sum(len(item.media_evidence.items) for item in media),
        omitted_images=sum(item.media_evidence.omitted_count for item in media),
        downloaded_bytes=sum(_downloaded_bytes(item.media_evidence) for item in media),
        ocr_characters=sum(_ocr_characters(item.media_evidence) for item in media),
        partial=omitted > 0 or any(item.media_evidence.partial for item in media),
    )


def _downloaded_bytes(evidence: MediaEvidence) -> int:
    return sum(item.byte_length or 0 for item in evidence.items)


def _ocr_characters(evidence: MediaEvidence) -> int:
    return sum(
        len(item.ocr.text)
        for item in evidence.items
        if item.ocr is not None and item.ocr.text is not None
    )
