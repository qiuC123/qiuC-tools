"""Discovery orchestration from sanitized search hits to optional article evidence."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import HttpUrl

from wxcli.browser_policy import BrowserDecision
from wxcli.discovery.hydration import HydrationCoordinator
from wxcli.discovery.identity import article_identity, query_fingerprint
from wxcli.discovery.models import (
    ArticleCandidate,
    CandidateConfidence,
    DiscoveryRequest,
    DiscoveryResult,
    DiscoverySummary,
    HydrationDecision,
    SearchProvenance,
    VerificationStatus,
)
from wxcli.discovery.provider import DiscoveryProvider
from wxcli.discovery.ranking import choose_hydration, rank_candidates
from wxcli.discovery.store import DiscoveryStore
from wxcli.discovery.tokens import (
    decode_checkpoint,
    decode_cursor,
    encode_checkpoint,
    encode_cursor,
)
from wxcli.errors import ErrorCode, WxcliError
from wxcli.evidence import EvidenceService


class DiscoveryService:
    """Keep CLI concerns outside the reusable direct-discovery workflow."""

    def __init__(
        self,
        provider: DiscoveryProvider,
        store: DiscoveryStore,
        http_evidence: EvidenceService | None = None,
        browser_evidence: EvidenceService | None = None,
        browser_decision: BrowserDecision | None = None,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._provider = provider
        if (
            isinstance(provider.page_size, bool)
            or not isinstance(provider.page_size, int)
            or provider.page_size < 1
            or provider.page_size > 100
        ):
            raise WxcliError(
                ErrorCode.LOCAL_CONFIGURATION_ERROR,
                "The discovery provider page size is invalid.",
            )
        self._page_size = provider.page_size
        self._store = store
        self._now = now
        self._browser_decision = browser_decision
        self._hydration = HydrationCoordinator(
            http_evidence,
            browser_evidence,
            now=now,
            monotonic=monotonic,
        )

    def search(self, request: DiscoveryRequest) -> DiscoveryResult:
        run_started = self._now().astimezone(UTC)
        fingerprint = query_fingerprint(request, self._provider.name)
        position = (
            decode_cursor(request.cursor, self._provider.name, fingerprint)
            if request.cursor
            else 0
        )
        page_offset = position // self._page_size
        skip_in_page = position % self._page_size
        checkpoint_time = (
            decode_checkpoint(request.checkpoint, self._provider.name, fingerprint)
            if request.checkpoint
            else None
        )
        self._store.prune(run_started)
        candidates: list[ArticleCandidate] = []
        identities: set[str] = set()
        received = 0
        duplicates = 0
        more_available = False
        next_position = position

        while len(candidates) < request.limit:
            page = self._store.get_page(
                self._provider.name, fingerprint, page_offset, run_started
            )
            if page is None:
                page = self._provider.search_page(
                    request, offset=page_offset, count=self._page_size
                )
                self._store.put_page(
                    self._provider.name, fingerprint, page_offset, page, run_started
                )
            visible_hits = page.hits[skip_in_page:]
            received += len(visible_hits)
            reached_limit = False
            for index, hit in enumerate(visible_hits, start=skip_in_page):
                next_position = page_offset * self._page_size + index + 1
                try:
                    fetch_url, identity = article_identity(hit.url)
                except WxcliError:
                    continue
                if identity in identities:
                    duplicates += 1
                    continue
                identities.add(identity)
                first_seen, last_seen, is_new = self._store.observe_candidate(
                    fingerprint, identity, fetch_url, self._now()
                )
                if checkpoint_time is not None and first_seen <= checkpoint_time:
                    continue
                if request.new_only and checkpoint_time is None and not is_new:
                    continue
                candidates.append(
                    ArticleCandidate(
                        fetch_url=HttpUrl(fetch_url),
                        article_identity=identity,
                        title_hint=hit.title,
                        account_hint=hit.account_hint,
                        snippet=hit.snippet,
                        backend_date_hint=hit.backend_date_hint,
                        discovered_at=first_seen,
                        last_seen_at=last_seen,
                        search_provenance=SearchProvenance(
                            provider=self._provider.name,
                            rank=hit.rank,
                            result_id=hit.result_id,
                        ),
                        confidence=CandidateConfidence.LOW,
                    )
                )
                if len(candidates) >= request.limit:
                    more_available = index + 1 < len(page.hits) or page.has_more
                    reached_limit = True
                    break
            if reached_limit:
                break
            if not page.has_more or page.next_offset is None:
                more_available = False
                break
            page_offset = page.next_offset
            skip_in_page = 0
            next_position = page_offset * self._page_size

        candidates = rank_candidates(candidates, request)
        choose_hydration(candidates, request)
        attempted = 0
        verified = 0
        partial = False
        browser_summary = None
        if request.hydrate:
            browser_summary = self._hydration.hydrate(
                candidates,
                request,
                self._browser_decision,
            )
            attempted = sum(
                item.hydration_decision != HydrationDecision.CANDIDATE_ONLY
                for item in candidates
            )
            verified = sum(
                item.verification_status == VerificationStatus.VERIFIED
                for item in candidates
            )
            partial = any(
                item.hydration_decision != HydrationDecision.CANDIDATE_ONLY
                and item.verification_status != VerificationStatus.VERIFIED
                for item in candidates
            )
            candidates = self._hydration.apply_strict_filters(candidates, request)
        run_finished = self._now().astimezone(UTC)
        self._store.put_checkpoint(self._provider.name, fingerprint, run_finished)
        return DiscoveryResult(
            search_provider=self._provider.name,
            next_cursor=(
                encode_cursor(self._provider.name, fingerprint, next_position)
                if more_available
                else None
            ),
            checkpoint=encode_checkpoint(self._provider.name, fingerprint, run_finished),
            summary=DiscoverySummary(
                received=received,
                accepted=len(candidates),
                duplicates_removed=duplicates,
                hydration_attempted=attempted,
                verified=verified,
                partial=partial,
            ),
            candidates=candidates,
            browser_fallback=browser_summary,
        )


def validate_discovery_tokens(
    request: DiscoveryRequest, provider_name: str = "brave"
) -> None:
    """Reject malformed or query-mismatched tokens before credentials are accessed."""
    fingerprint = query_fingerprint(request, provider_name)
    if request.cursor:
        decode_cursor(request.cursor, provider_name, fingerprint)
    if request.checkpoint:
        decode_checkpoint(request.checkpoint, provider_name, fingerprint)
