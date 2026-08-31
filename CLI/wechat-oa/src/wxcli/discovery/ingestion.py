"""Agent-orchestrated Candidate Batch validation, deduplication, and Hydration."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import HttpUrl

from wxcli.browser_policy import BrowserDecision
from wxcli.discovery.hydration import HydrationCoordinator
from wxcli.discovery.identity import article_identity, query_fingerprint
from wxcli.discovery.models import (
    ArticleCandidate,
    CandidateBatchRequest,
    CandidateConfidence,
    CandidateIngestionResult,
    CandidateIngestionSummary,
    CandidateInput,
    CandidateRejection,
    DiscoveryRequest,
    HydrationDecision,
    SearchProvenance,
    VerificationStatus,
)
from wxcli.discovery.ranking import choose_hydration, rank_candidates
from wxcli.discovery.store import DiscoveryStore
from wxcli.errors import WxcliError
from wxcli.evidence import EvidenceService
from wxcli.redaction import redact_text


class CandidateIngestionService:
    """Treat an orchestrator batch as untrusted hints and produce wxcli evidence."""

    def __init__(
        self,
        store: DiscoveryStore,
        http_evidence: EvidenceService | None,
        browser_evidence: EvidenceService | None = None,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._now = now
        self._hydration = HydrationCoordinator(
            http_evidence,
            browser_evidence,
            now=now,
            monotonic=monotonic,
        )

    def ingest(
        self,
        batch: CandidateBatchRequest,
        *,
        priority_hydrate: int | None = None,
        max_hydrate: int | None = None,
        require_account_match: bool = False,
        require_published_date: bool = False,
        allow_browser: bool = False,
        browser_decision: BrowserDecision | None = None,
    ) -> CandidateIngestionResult:
        policy = batch.hydration
        request = DiscoveryRequest(
            query=batch.discovery_request.query,
            companies=batch.discovery_request.companies,
            expected_accounts=batch.discovery_request.expected_accounts,
            published_after=batch.discovery_request.published_after,
            published_before=batch.discovery_request.published_before,
            hydrate=True,
            priority_hydrate=(
                policy.priority_count
                if priority_hydrate is None
                else priority_hydrate
            ),
            max_hydrate=(
                policy.maximum_attempts if max_hydrate is None else max_hydrate
            ),
            require_account_match=require_account_match,
            require_published_date=require_published_date,
            allow_browser=allow_browser,
        )
        provider_key = "agent:" + batch.source.orchestrator + ":" + ",".join(
            sorted(batch.source.providers)
        )
        fingerprint = query_fingerprint(request, provider_key)
        run_started = self._now().astimezone(UTC)
        self._store.prune(run_started)
        accepted_inputs: dict[str, tuple[int, CandidateInput, str]] = {}
        rejections: list[CandidateRejection] = []
        duplicates = 0

        for index, candidate_input in enumerate(batch.candidates):
            try:
                fetch_url, identity = article_identity(candidate_input.url)
            except WxcliError as error:
                rejections.append(
                    CandidateRejection(
                        index=index,
                        error_code=error.code,
                        message=redact_text(error.message),
                    )
                )
                continue
            previous = accepted_inputs.get(identity)
            if previous is not None:
                duplicates += 1
                previous_index, previous_input, _ = previous
                if (candidate_input.search_provenance.rank, index) >= (
                    previous_input.search_provenance.rank,
                    previous_index,
                ):
                    continue
            accepted_inputs[identity] = (index, candidate_input, fetch_url)

        candidates: list[ArticleCandidate] = []
        for identity, (_, candidate_input, fetch_url) in accepted_inputs.items():
            first_seen, last_seen, _ = self._store.observe_candidate(
                fingerprint,
                identity,
                fetch_url,
                self._now(),
            )
            provenance = candidate_input.search_provenance
            candidates.append(
                ArticleCandidate(
                    fetch_url=HttpUrl(fetch_url),
                    article_identity=identity,
                    title_hint=candidate_input.title_hint,
                    account_hint=candidate_input.account_hint,
                    snippet=candidate_input.snippet,
                    backend_date_hint=candidate_input.backend_date_hint,
                    discovered_at=first_seen,
                    last_seen_at=last_seen,
                    search_provenance=SearchProvenance(
                        provider=provenance.provider,
                        rank=provenance.rank,
                        result_id=(
                            provenance.result_id
                            or hashlib.sha256(fetch_url.encode("utf-8")).hexdigest()[:24]
                        ),
                    ),
                    confidence=CandidateConfidence.LOW,
                )
            )

        candidates = rank_candidates(candidates, request)
        choose_hydration(candidates, request)
        browser_summary = self._hydration.hydrate(
            candidates,
            request,
            browser_decision,
        )
        attempted = sum(
            item.hydration_decision != HydrationDecision.CANDIDATE_ONLY
            for item in candidates
        )
        verified = sum(
            item.verification_status == VerificationStatus.VERIFIED
            for item in candidates
        )
        hydration_partial = any(
            item.hydration_decision != HydrationDecision.CANDIDATE_ONLY
            and item.verification_status != VerificationStatus.VERIFIED
            for item in candidates
        )
        candidates = self._hydration.apply_strict_filters(candidates, request)
        partial = bool(rejections) or hydration_partial
        return CandidateIngestionResult(
            orchestrator=batch.source.orchestrator,
            summary=CandidateIngestionSummary(
                received=len(batch.candidates),
                accepted=len(candidates),
                duplicates_removed=duplicates,
                invalid_removed=len(rejections),
                hydration_attempted=attempted,
                verified=verified,
                partial=partial,
            ),
            rejections=rejections,
            candidates=candidates,
            browser_fallback=browser_summary,
        )
