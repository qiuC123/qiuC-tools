"""Versioned public models for WeChat article discovery."""

from __future__ import annotations

import re
from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from wxcli.errors import ErrorCode
from wxcli.browser_policy import BrowserMode, BrowserPolicySource
from wxcli.evidence import ArticleEvidence, ExpectedAccount
from wxcli.models import Provider
from wxcli.redaction import contains_credential_assignment

DISCOVERY_SCHEMA_VERSION: Literal["1"] = "1"
_MAX_HINT_LENGTH = 200
_MAX_OUTBOUND_QUERY_LENGTH = 2_000
MAX_CANDIDATE_BATCH_ITEMS = 100
MAX_CANDIDATE_BATCH_BYTES = 2 * 1024 * 1024
_IDENTIFIER_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"


class CandidateConfidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class HydrationDecision(StrEnum):
    PRIORITY = "priority"
    SELECTED = "selected"
    CANDIDATE_ONLY = "candidate_only"


class VerificationStatus(StrEnum):
    NOT_ATTEMPTED = "not_attempted"
    VERIFIED = "verified"
    VERIFICATION_REQUIRED = "verification_required"
    NOT_FOUND = "not_found"
    PARSE_FAILED = "parse_failed"
    NETWORK_FAILED = "network_failed"


class DiscoveryRequest(BaseModel):
    """Schema-v1 request accepted by both CLI flags and JSON pipelines."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = DISCOVERY_SCHEMA_VERSION
    query: str = Field(min_length=1, max_length=500)
    companies: list[str] = Field(default_factory=list, max_length=100)
    expected_accounts: list[ExpectedAccount] = Field(default_factory=list, max_length=100)
    published_after: date | None = None
    published_before: date | None = None
    limit: int = Field(default=50, ge=1, le=50)
    cursor: str | None = Field(default=None, max_length=4096)
    checkpoint: str | None = Field(default=None, max_length=4096)
    new_only: bool = False
    hydrate: bool = False
    priority_hydrate: int = Field(default=10, ge=0, le=20)
    max_hydrate: int = Field(default=20, ge=0, le=20)
    require_account_match: bool = False
    require_published_date: bool = False
    allow_browser: bool = False

    @model_validator(mode="after")
    def validate_request(self) -> DiscoveryRequest:
        self.query = self.query.strip()
        if not self.query:
            raise ValueError("The discovery query must not be empty.")
        _validate_search_text(self.query, "query", 500)
        self.companies = _clean_unique(self.companies)
        for company in self.companies:
            _validate_search_text(company, "company hint", _MAX_HINT_LENGTH)
        outbound_length = len("site:mp.weixin.qq.com/s ") + len(self.query)
        outbound_length += sum(len(company) + 3 for company in self.companies)
        outbound_length += sum(
            len(name) + 3
            for account in self.expected_accounts
            for name in account.display_names
        )
        if outbound_length > _MAX_OUTBOUND_QUERY_LENGTH:
            raise ValueError("The combined discovery query is too long.")
        if self.published_after and self.published_before:
            if self.published_after > self.published_before:
                raise ValueError("published_after must not be later than published_before.")
        if self.priority_hydrate > self.max_hydrate:
            raise ValueError("priority_hydrate must not exceed max_hydrate.")
        if self.allow_browser and not self.hydrate:
            raise ValueError("Browser fallback requires hydration.")
        if (self.require_account_match or self.require_published_date) and not self.hydrate:
            raise ValueError("Strict source filters require hydration.")
        if self.cursor and (self.checkpoint or self.new_only):
            raise ValueError("cursor cannot be combined with checkpoint or new_only.")
        return self


class SearchProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(pattern=_IDENTIFIER_PATTERN)
    rank: int = Field(ge=1)
    result_id: str = Field(min_length=1, max_length=128)


class CandidateBatchDiscoveryRequest(BaseModel):
    """Search criteria reported by an external orchestrator, never source evidence."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    companies: list[str] = Field(default_factory=list, max_length=100)
    expected_accounts: list[ExpectedAccount] = Field(default_factory=list, max_length=100)
    published_after: date | None = None
    published_before: date | None = None

    @model_validator(mode="after")
    def validate_criteria(self) -> CandidateBatchDiscoveryRequest:
        self.query = self.query.strip()
        _validate_search_text(self.query, "query", 500)
        self.companies = _clean_unique(self.companies)
        for company in self.companies:
            _validate_search_text(company, "company hint", _MAX_HINT_LENGTH)
        if self.published_after and self.published_before:
            if self.published_after > self.published_before:
                raise ValueError("published_after must not be later than published_before.")
        return self


class CandidateBatchSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    orchestrator: str = Field(pattern=_IDENTIFIER_PATTERN)
    providers: list[str] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def normalize_source(self) -> CandidateBatchSource:
        self.orchestrator = self.orchestrator.casefold()
        self.providers = _clean_unique([value.casefold() for value in self.providers])
        if not self.providers:
            raise ValueError("At least one external discovery provider is required.")
        for provider in self.providers:
            _validate_identifier(provider, "external discovery provider")
        return self


class ExternalSearchProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(pattern=_IDENTIFIER_PATTERN)
    rank: int = Field(ge=1)
    result_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def normalize_provenance(self) -> ExternalSearchProvenance:
        self.provider = self.provider.casefold()
        if self.result_id is not None:
            self.result_id = self.result_id.strip() or None
            if self.result_id is not None:
                _validate_hint_text(self.result_id, "result_id")
        return self


class CandidateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=4096)
    title_hint: str | None = Field(default=None, max_length=500)
    account_hint: str | None = Field(default=None, max_length=200)
    snippet: str | None = Field(default=None, max_length=5000)
    backend_date_hint: date | None = None
    search_provenance: ExternalSearchProvenance

    @model_validator(mode="after")
    def normalize_hints(self) -> CandidateInput:
        self.url = self.url.strip()
        if not self.url:
            raise ValueError("Candidate URL must not be empty.")
        for field_name in ("title_hint", "account_hint", "snippet"):
            value = getattr(self, field_name)
            if value is not None:
                clean = value.strip() or None
                if clean is not None:
                    _validate_hint_text(clean, field_name)
                setattr(self, field_name, clean)
        return self


class CandidateHydrationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    priority_count: int = Field(default=10, ge=0, le=20)
    maximum_attempts: int = Field(default=20, ge=0, le=20)

    @model_validator(mode="after")
    def validate_counts(self) -> CandidateHydrationPolicy:
        if self.priority_count > self.maximum_attempts:
            raise ValueError("priority_count must not exceed maximum_attempts.")
        return self


class CandidateBatchRequest(BaseModel):
    """Strict, bounded Candidate Batch accepted from an external orchestrator."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = DISCOVERY_SCHEMA_VERSION
    discovery_request: CandidateBatchDiscoveryRequest
    source: CandidateBatchSource
    candidates: list[CandidateInput] = Field(max_length=MAX_CANDIDATE_BATCH_ITEMS)
    hydration: CandidateHydrationPolicy = Field(default_factory=CandidateHydrationPolicy)

    @model_validator(mode="after")
    def validate_candidate_sources(self) -> CandidateBatchRequest:
        declared = set(self.source.providers)
        if any(
            candidate.search_provenance.provider not in declared
            for candidate in self.candidates
        ):
            raise ValueError("Every candidate provider must be declared by the batch source.")
        if any(
            contains_credential_assignment(value)
            for value in _all_strings(self.model_dump(mode="json"))
        ):
            raise ValueError("Candidate Batch text must not contain credential assignments.")
        return self


class HydrationAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Provider
    attempted_at: datetime
    verification_status: VerificationStatus
    error_code: ErrorCode
    message: str
    verification_stage: Literal["browser"] | None = None
    required_action: Literal["run_browser_login"] | None = None


class BrowserFallbackSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effective_mode: BrowserMode
    policy_source: BrowserPolicySource
    eligible: int = Field(ge=0)
    attempted: int = Field(ge=0)
    verified: int = Field(ge=0)
    user_action_required: int = Field(ge=0)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    warning: Literal["browser_policy_invalid"] | None = None


class ArticleCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fetch_url: HttpUrl
    article_identity: str
    title_hint: str | None = None
    account_hint: str | None = None
    snippet: str | None = None
    backend_date_hint: date | None = None
    discovered_at: datetime
    last_seen_at: datetime
    search_provenance: SearchProvenance
    match_reasons: list[str] = Field(default_factory=list)
    confidence: CandidateConfidence
    hydration_decision: HydrationDecision = HydrationDecision.CANDIDATE_ONLY
    hydration_decision_reasons: list[str] = Field(default_factory=list)
    verification_status: VerificationStatus = VerificationStatus.NOT_ATTEMPTED
    evidence: ArticleEvidence | None = None
    hydration_attempt: HydrationAttempt | None = None

    @model_validator(mode="after")
    def validate_hydration_outcome(self) -> ArticleCandidate:
        if self.evidence is not None and self.hydration_attempt is not None:
            raise ValueError("A candidate cannot contain both evidence and a failed attempt.")
        if self.verification_status == VerificationStatus.VERIFIED and self.evidence is None:
            raise ValueError("Verified candidates require article evidence.")
        if self.evidence is not None and self.verification_status != VerificationStatus.VERIFIED:
            raise ValueError("Article evidence requires verified status.")
        return self


class DiscoverySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    received: int = Field(ge=0)
    accepted: int = Field(ge=0)
    duplicates_removed: int = Field(ge=0)
    hydration_attempted: int = Field(ge=0)
    verified: int = Field(ge=0)
    partial: bool


class DiscoveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = DISCOVERY_SCHEMA_VERSION
    search_provider: str = Field(pattern=_IDENTIFIER_PATTERN)
    next_cursor: str | None = None
    checkpoint: str
    summary: DiscoverySummary
    candidates: list[ArticleCandidate]
    browser_fallback: BrowserFallbackSummary | None = None


class CandidateRejection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    error_code: ErrorCode
    message: str


class CandidateIngestionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    received: int = Field(ge=0)
    accepted: int = Field(ge=0)
    duplicates_removed: int = Field(ge=0)
    invalid_removed: int = Field(ge=0)
    hydration_attempted: int = Field(ge=0)
    verified: int = Field(ge=0)
    partial: bool


class CandidateIngestionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = DISCOVERY_SCHEMA_VERSION
    discovery_mode: Literal["agent_orchestrated"] = "agent_orchestrated"
    orchestrator: str
    provenance_trust: Literal["orchestrator_reported"] = "orchestrator_reported"
    summary: CandidateIngestionSummary
    rejections: list[CandidateRejection]
    candidates: list[ArticleCandidate]
    browser_fallback: BrowserFallbackSummary | None = None


class SearchHit(BaseModel):
    """Sanitized provider output; raw provider responses never leave the provider."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=500)
    url: str = Field(max_length=4096)
    snippet: str | None = Field(default=None, max_length=5000)
    account_hint: str | None = Field(default=None, max_length=200)
    backend_date_hint: date | None = None
    rank: int = Field(ge=1)
    result_id: str = Field(max_length=128)


class SearchPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hits: list[SearchHit]
    has_more: bool
    next_offset: int | None = Field(default=None, ge=0)


def _clean_unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = value.strip()
        key = clean.casefold()
        if clean and key not in seen:
            result.append(clean)
            seen.add(key)
    return result


def _validate_search_text(value: str, label: str, maximum: int) -> None:
    if len(value) > maximum:
        raise ValueError(f"The {label} is limited to {maximum} characters.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"The {label} must not contain control characters.")


def _validate_hint_text(value: str, label: str) -> None:
    if any(
        (ord(character) < 32 and character not in "\t\n\r")
        or ord(character) == 127
        for character in value
    ):
        raise ValueError(f"The {label} must not contain unsafe control characters.")


def _validate_identifier(value: str, label: str) -> None:
    if re.fullmatch(_IDENTIFIER_PATTERN, value) is None:
        raise ValueError(f"The {label} identifier is invalid.")


def _all_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _all_strings(child)]
    if isinstance(value, list):
        return [item for child in value for item in _all_strings(child)]
    return []
