"""Shared, bounded conversion of Article Candidates into Article Evidence."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import UTC, datetime

from wxcli.discovery.models import (
    ArticleCandidate,
    CandidateConfidence,
    DiscoveryRequest,
    HydrationAttempt,
    HydrationDecision,
    VerificationStatus,
)
from wxcli.errors import ErrorCode, WxcliError
from wxcli.evidence import EvidenceService, IdentityStatus, reclassify_account_identity
from wxcli.models import Provider
from wxcli.redaction import redact_text


class HydrationCoordinator:
    """Run the same evidence workflow for direct and agent-orchestrated discovery."""

    def __init__(
        self,
        http_evidence: EvidenceService | None,
        browser_evidence: EvidenceService | None,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._http_evidence = http_evidence
        self._browser_evidence = browser_evidence
        self._now = now

    def hydrate(
        self, candidates: list[ArticleCandidate], request: DiscoveryRequest
    ) -> None:
        if self._http_evidence is None:
            raise WxcliError(
                ErrorCode.LOCAL_CONFIGURATION_ERROR,
                "The article evidence service is unavailable.",
            )
        selected = [
            item
            for item in candidates
            if item.hydration_decision != HydrationDecision.CANDIDATE_ONLY
        ]
        futures: dict[Future[object], ArticleCandidate] = {}
        executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="wxcli-hydrate")
        try:
            for candidate in selected:
                futures[executor.submit(self._hydrate_http, candidate, request)] = candidate
            try:
                for future in as_completed(futures, timeout=300.0):
                    candidate = futures[future]
                    try:
                        future.result()
                    except Exception:
                        self._record_failure(
                            candidate,
                            Provider.HTTP,
                            _unexpected_evidence_error(),
                        )
            except FuturesTimeoutError:
                for future, candidate in futures.items():
                    if not future.done():
                        future.cancel()
                        self._record_failure(
                            candidate,
                            Provider.HTTP,
                            WxcliError(
                                ErrorCode.NETWORK_ERROR,
                                "The article hydration batch deadline was reached.",
                            ),
                        )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        if request.allow_browser and self._browser_evidence is not None:
            for candidate in selected:
                if candidate.verification_status == VerificationStatus.VERIFICATION_REQUIRED:
                    self._hydrate_browser(candidate, request)

    def _hydrate_http(
        self, candidate: ArticleCandidate, request: DiscoveryRequest
    ) -> None:
        assert self._http_evidence is not None
        for attempt in range(2):
            try:
                evidence = self._http_evidence.get(
                    str(candidate.fetch_url), request.expected_accounts
                )
                candidate.evidence = evidence
                candidate.hydration_attempt = None
                candidate.verification_status = VerificationStatus.VERIFIED
                self._mark_repost_if_needed(candidate, request)
                self._recalibrate_confidence(candidate)
                return
            except WxcliError as error:
                if error.code == ErrorCode.NETWORK_ERROR and attempt == 0:
                    continue
                self._record_failure(candidate, Provider.HTTP, error)
                return
            except Exception:
                self._record_failure(
                    candidate,
                    Provider.HTTP,
                    _unexpected_evidence_error(),
                )
                return

    def _hydrate_browser(
        self, candidate: ArticleCandidate, request: DiscoveryRequest
    ) -> None:
        assert self._browser_evidence is not None
        try:
            evidence = self._browser_evidence.get(
                str(candidate.fetch_url), request.expected_accounts
            )
            candidate.evidence = evidence
            candidate.hydration_attempt = None
            candidate.verification_status = VerificationStatus.VERIFIED
            self._mark_repost_if_needed(candidate, request)
            self._recalibrate_confidence(candidate)
        except WxcliError as error:
            status = (
                VerificationStatus.VERIFICATION_REQUIRED
                if error.code == ErrorCode.CHROME_ERROR
                else _verification_status(error.code)
            )
            candidate.evidence = None
            candidate.verification_status = status
            candidate.hydration_attempt = HydrationAttempt(
                provider=Provider.CHROME,
                attempted_at=self._now(),
                verification_status=status,
                error_code=error.code,
                message=redact_text(error.message),
            )
        except Exception:
            candidate.evidence = None
            candidate.verification_status = VerificationStatus.VERIFICATION_REQUIRED
            candidate.hydration_attempt = HydrationAttempt(
                provider=Provider.CHROME,
                attempted_at=self._now(),
                verification_status=VerificationStatus.VERIFICATION_REQUIRED,
                error_code=ErrorCode.CHROME_ERROR,
                message="The browser evidence pipeline failed unexpectedly.",
            )

    def _record_failure(
        self, candidate: ArticleCandidate, provider: Provider, error: WxcliError
    ) -> None:
        status = _verification_status(error.code)
        candidate.evidence = None
        candidate.verification_status = status
        candidate.hydration_attempt = HydrationAttempt(
            provider=provider,
            attempted_at=self._now(),
            verification_status=status,
            error_code=error.code,
            message=redact_text(error.message),
        )

    @staticmethod
    def _mark_repost_if_needed(
        candidate: ArticleCandidate, request: DiscoveryRequest
    ) -> None:
        assert candidate.evidence is not None
        if candidate.evidence.account_identity.status != IdentityStatus.MISMATCH:
            return
        hint = "".join((candidate.account_hint or "").split()).casefold()
        expected_names = {
            "".join(name.split()).casefold()
            for account in request.expected_accounts
            for name in account.display_names
        }
        if hint and hint in expected_names:
            candidate.evidence = reclassify_account_identity(
                candidate.evidence, IdentityStatus.REPOST_SUSPECTED
            )

    @staticmethod
    def _recalibrate_confidence(candidate: ArticleCandidate) -> None:
        assert candidate.evidence is not None
        status = candidate.evidence.account_identity.status
        if status == IdentityStatus.ALLOWLIST_MATCHED:
            candidate.confidence = CandidateConfidence.HIGH
        elif status in {IdentityStatus.NAME_ONLY_MATCHED, IdentityStatus.OBSERVED}:
            candidate.confidence = CandidateConfidence.MEDIUM
        else:
            candidate.confidence = CandidateConfidence.LOW

    @staticmethod
    def apply_strict_filters(
        candidates: list[ArticleCandidate], request: DiscoveryRequest
    ) -> list[ArticleCandidate]:
        result: list[ArticleCandidate] = []
        for candidate in candidates:
            evidence = candidate.evidence
            if evidence is None:
                if request.require_account_match or request.require_published_date:
                    continue
                result.append(candidate)
                continue
            if request.require_account_match and evidence.account_identity.status not in {
                IdentityStatus.ALLOWLIST_MATCHED,
                IdentityStatus.NAME_ONLY_MATCHED,
            }:
                continue
            published = evidence.article.published_at
            if request.require_published_date and published is None:
                continue
            if published is not None:
                published_date = published.date()
                if request.published_after and published_date < request.published_after:
                    continue
                if request.published_before and published_date > request.published_before:
                    continue
            result.append(candidate)
        return result


def _verification_status(code: ErrorCode) -> VerificationStatus:
    if code == ErrorCode.VERIFICATION_REQUIRED:
        return VerificationStatus.VERIFICATION_REQUIRED
    if code == ErrorCode.NOT_FOUND:
        return VerificationStatus.NOT_FOUND
    if code == ErrorCode.NETWORK_ERROR:
        return VerificationStatus.NETWORK_FAILED
    return VerificationStatus.PARSE_FAILED


def _unexpected_evidence_error() -> WxcliError:
    return WxcliError(
        ErrorCode.PARSING_ERROR,
        "The article evidence pipeline failed unexpectedly.",
    )
