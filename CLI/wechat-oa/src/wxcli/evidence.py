"""Public evidence models and evidence construction for readable WeChat articles."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from collections.abc import Iterator
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from wxcli.models import Article, Provider
from wxcli.public_article import PublicArticleDocument

EVIDENCE_SCHEMA_VERSION: Literal["1"] = "1"
EVIDENCE_EXTRACTOR_VERSION = "1"


class IdentityStatus(StrEnum):
    OBSERVED = "observed"
    ALLOWLIST_MATCHED = "allowlist_matched"
    NAME_ONLY_MATCHED = "name_only_matched"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"
    REPOST_SUSPECTED = "repost_suspected"


class ExternalLinkType(StrEnum):
    WECHAT = "wechat"
    EXTERNAL_HTTP = "external_http"
    EMAIL = "email"
    PHONE = "phone"


class ExpectedAccount(BaseModel):
    """Caller-owned expected public account identity."""

    model_config = ConfigDict(extra="forbid")

    biz_id: str | None = Field(default=None, max_length=512)
    display_names: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_identity(self) -> ExpectedAccount:
        if self.biz_id is not None:
            self.biz_id = self.biz_id.strip() or None
        self.display_names = [name.strip() for name in self.display_names if name.strip()]
        if any(len(name) > 200 for name in self.display_names):
            raise ValueError("Expected account display names are limited to 200 characters.")
        if any(_has_control_characters(name) for name in self.display_names):
            raise ValueError("Expected account display names must not contain control characters.")
        if not self.biz_id and not self.display_names:
            raise ValueError("An expected account requires a biz_id or display name.")
        return self


class AccountIdentityEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observed_display_name: str | None = None
    observed_biz_id: str | None = None
    status: IdentityStatus
    matched_by: list[str] = Field(default_factory=list)
    matched_expected_index: int | None = Field(default=None, ge=0)


class ExternalLinkEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    source_location: str
    raw_value: str
    normalized_value: str
    kind: ExternalLinkType
    text: str | None = None


class ImageEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    url: str


class ArticleEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = EVIDENCE_SCHEMA_VERSION
    article: Article
    account_identity: AccountIdentityEvidence
    external_links: list[ExternalLinkEvidence] = Field(default_factory=list)
    images: list[ImageEvidence] = Field(default_factory=list)
    last_verified_at: datetime
    content_sha256: str
    evidence_sha256: str


class EvidenceDocumentProvider(Protocol):
    def get_document(
        self,
        url: str,
        *,
        no_cache: bool = False,
    ) -> PublicArticleDocument: ...


class EvidenceService:
    """Create auditable evidence from a content provider document."""

    def __init__(self, provider: EvidenceDocumentProvider) -> None:
        self._provider = provider

    def get(
        self,
        url: str,
        expected_accounts: list[ExpectedAccount] | None = None,
        *,
        no_cache: bool = False,
    ) -> ArticleEvidence:
        document = (
            self._provider.get_document(url, no_cache=True)
            if no_cache
            else self._provider.get_document(url)
        )
        return build_article_evidence(document, expected_accounts or [])

    @contextmanager
    def batch(self, *, timeout_seconds: float | None = None) -> Iterator[EvidenceService]:
        """Provide a reusable evidence reader; non-browser providers need no setup."""
        del timeout_seconds
        yield self


def build_article_evidence(
    document: PublicArticleDocument,
    expected_accounts: list[ExpectedAccount],
    *,
    now: datetime | None = None,
) -> ArticleEvidence:
    identity = compare_account_identity(document, expected_accounts)
    links = [
        ExternalLinkEvidence(
            index=link.index,
            source_location=link.source_location,
            raw_value=link.raw_value,
            normalized_value=link.normalized_value,
            kind=ExternalLinkType(link.kind),
            text=link.text,
        )
        for link in document.external_links
    ]
    images = [ImageEvidence(index=index, url=url) for index, url in enumerate(document.article.images)]
    content_payload = {
        "title": document.article.title,
        "content_markdown": _normalize_text(document.article.content_markdown),
        "author": document.article.author,
        "published_at": (
            document.article.published_at.isoformat() if document.article.published_at else None
        ),
        "account": {
            "display_name": identity.observed_display_name,
            "biz_id": identity.observed_biz_id,
        },
        "external_links": [link.model_dump(mode="json") for link in links],
        "images": [image.model_dump(mode="json") for image in images],
    }
    content_sha256 = _json_sha256(content_payload)
    return ArticleEvidence(
        article=document.article,
        account_identity=identity,
        external_links=links,
        images=images,
        last_verified_at=now or datetime.now(UTC),
        content_sha256=content_sha256,
        evidence_sha256=_evidence_hash(
            content_sha256, document.article.provider, identity, links
        ),
    )


def reclassify_account_identity(
    evidence: ArticleEvidence, status: IdentityStatus
) -> ArticleEvidence:
    """Return evidence with a derived identity status and a matching evidence hash."""
    identity = evidence.account_identity.model_copy(update={"status": status})
    return evidence.model_copy(
        update={
            "account_identity": identity,
            "evidence_sha256": _evidence_hash(
                evidence.content_sha256,
                evidence.article.provider,
                identity,
                evidence.external_links,
            ),
        }
    )


def compare_account_identity(
    document: PublicArticleDocument,
    expected_accounts: list[ExpectedAccount],
) -> AccountIdentityEvidence:
    observed_name = document.account_display_name
    observed_biz = document.account_biz_id
    if not expected_accounts:
        status = IdentityStatus.OBSERVED if observed_name or observed_biz else IdentityStatus.UNKNOWN
        return AccountIdentityEvidence(
            observed_display_name=observed_name,
            observed_biz_id=observed_biz,
            status=status,
        )

    for index, expected in enumerate(expected_accounts):
        if observed_biz and expected.biz_id and observed_biz == expected.biz_id:
            return AccountIdentityEvidence(
                observed_display_name=observed_name,
                observed_biz_id=observed_biz,
                status=IdentityStatus.ALLOWLIST_MATCHED,
                matched_by=["biz_id"],
                matched_expected_index=index,
            )

    normalized_observed = _normalize_name(observed_name) if observed_name else None
    for index, expected in enumerate(expected_accounts):
        if normalized_observed and normalized_observed in {
            _normalize_name(name) for name in expected.display_names
        }:
            return AccountIdentityEvidence(
                observed_display_name=observed_name,
                observed_biz_id=observed_biz,
                status=IdentityStatus.NAME_ONLY_MATCHED,
                matched_by=["display_name"],
                matched_expected_index=index,
            )

    status = IdentityStatus.MISMATCH if observed_name or observed_biz else IdentityStatus.UNKNOWN
    return AccountIdentityEvidence(
        observed_display_name=observed_name,
        observed_biz_id=observed_biz,
        status=status,
    )


def _normalize_name(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _has_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _normalize_text(value: str) -> str:
    return "\n".join(line.rstrip() for line in value.replace("\r\n", "\n").split("\n")).strip()


def _json_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _evidence_hash(
    content_sha256: str,
    provider: Provider,
    identity: AccountIdentityEvidence,
    links: list[ExternalLinkEvidence],
) -> str:
    return _json_sha256(
        {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "extractor_version": EVIDENCE_EXTRACTOR_VERSION,
            "content_sha256": content_sha256,
            "provider": provider,
            "account_identity": identity.model_dump(mode="json"),
            "external_links": [link.model_dump(mode="json") for link in links],
        }
    )
