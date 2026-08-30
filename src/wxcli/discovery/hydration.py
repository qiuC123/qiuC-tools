"""Shared, bounded conversion of Article Candidates into Article Evidence."""

from __future__ import annotations

import time
from contextlib import AbstractContextManager, contextmanager
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import UTC, datetime
from typing import Protocol, cast

from wxcli.browser_policy import BrowserDecision, BrowserMode, BrowserPolicySource
from wxcli.discovery.models import (
    ArticleCandidate,
    BrowserFallbackSummary,
    CandidateConfidence,
    DiscoveryRequest,
    HydrationAttempt,
    HydrationDecision,
    VerificationStatus,
)
from wxcli.errors import ErrorCode, VerificationRequiredError, WxcliError
from wxcli.evidence import IdentityStatus, reclassify_account_identity
from wxcli.evidence import ArticleEvidence, ExpectedAccount
from wxcli.models import Provider
from wxcli.redaction import redact_text


class EvidenceReader(Protocol):
    def get(
        self, url: str, expected_accounts: list[ExpectedAccount]
    ) -> ArticleEvidence: ...


class HydrationCoordinator:
    """Run the same evidence workflow for direct and agent-orchestrated discovery."""

    def __init__(
        self,
        http_evidence: EvidenceReader | None,
        browser_evidence: EvidenceReader | None,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http_evidence = http_evidence
        self._browser_evidence = browser_evidence
        self._now = now
        self._monotonic = monotonic

    def hydrate(
        self,
        candidates: list[ArticleCandidate],
        request: DiscoveryRequest,
        decision: BrowserDecision | None = None,
    ) -> BrowserFallbackSummary:
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
        effective_decision = decision or BrowserDecision(
            BrowserMode.AUTO_FALLBACK if request.allow_browser else BrowserMode.NEVER,
            BrowserPolicySource.REQUEST_JSON if request.allow_browser else BrowserPolicySource.DEFAULT,
        )
        run_deadline = self._monotonic() + 600.0
        futures: dict[Future[ArticleEvidence | WxcliError], ArticleCandidate] = {}
        executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="wxcli-hydrate")
        try:
            for candidate in selected:
                futures[executor.submit(self._hydrate_http, candidate, request)] = candidate
            try:
                http_timeout = max(0.001, min(300.0, run_deadline - self._monotonic()))
                for future in as_completed(futures, timeout=http_timeout):
                    candidate = futures[future]
                    try:
                        result = future.result()
                        if isinstance(result, WxcliError):
                            self._record_failure(candidate, Provider.HTTP, result)
                        else:
                            self._record_success(candidate, result, request)
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

        eligible = [
            candidate
            for candidate in selected
            if candidate.verification_status == VerificationStatus.VERIFICATION_REQUIRED
        ]
        summary = BrowserFallbackSummary(
            effective_mode=effective_decision.mode,
            policy_source=effective_decision.source,
            eligible=len(eligible),
            attempted=0,
            verified=0,
            user_action_required=0,
            warning=effective_decision.warning,
        )
        if not effective_decision.allows_fallback or not eligible:
            return summary
        if self._browser_evidence is None:
            return summary

        remaining = min(300.0, run_deadline - self._monotonic())
        if remaining <= 0:
            deadline_error = WxcliError(
                ErrorCode.CHROME_ERROR,
                "The hydration command deadline was reached.",
            )
            self._record_remaining_browser_failures(eligible, deadline_error)
            return summary

        started_at: datetime | None = None
        finished_at: datetime | None = None
        attempted = 0
        browser_verified = 0
        user_action_required = 0
        try:
            with _evidence_batch(self._browser_evidence, remaining) as browser_reader:
                started_at = self._now()
                for index, candidate in enumerate(eligible):
                    attempted += 1
                    attempt_error = self._hydrate_browser(browser_reader, candidate, request)
                    if attempt_error is None:
                        browser_verified += 1
                        continue
                    if attempt_error.code in {
                        ErrorCode.VERIFICATION_REQUIRED,
                        ErrorCode.CHROME_ERROR,
                        ErrorCode.BROWSER_BUSY,
                    }:
                        unvisited = eligible[index + 1 :]
                        if attempt_error.code == ErrorCode.VERIFICATION_REQUIRED:
                            user_action_required = 1 + len(unvisited)
                            pending: WxcliError = VerificationRequiredError(
                                "The browser session must be refreshed before reading more articles.",
                                verification_stage="browser",
                                required_action="run_browser_login",
                            )
                        else:
                            pending = attempt_error
                        self._record_remaining_browser_failures(unvisited, pending)
                        break
                finished_at = self._now()
        except WxcliError as error:
            self._record_remaining_browser_failures(eligible, error)
        except Exception:
            self._record_remaining_browser_failures(
                eligible,
                WxcliError(
                    ErrorCode.CHROME_ERROR,
                    "The browser evidence pipeline failed unexpectedly.",
                ),
            )
        finally:
            if started_at is not None and finished_at is None:
                finished_at = self._now()
        return BrowserFallbackSummary(
            effective_mode=effective_decision.mode,
            policy_source=effective_decision.source,
            eligible=len(eligible),
            attempted=attempted,
            verified=browser_verified,
            user_action_required=user_action_required,
            started_at=started_at,
            finished_at=finished_at,
            warning=effective_decision.warning,
        )

    def _hydrate_http(
        self, candidate: ArticleCandidate, request: DiscoveryRequest
    ) -> ArticleEvidence | WxcliError:
        assert self._http_evidence is not None
        for attempt in range(2):
            try:
                return self._http_evidence.get(
                    str(candidate.fetch_url), request.expected_accounts
                )
            except WxcliError as error:
                if error.code == ErrorCode.NETWORK_ERROR and attempt == 0:
                    continue
                return error
            except Exception:
                return _unexpected_evidence_error()
        return _unexpected_evidence_error()

    def _record_success(
        self,
        candidate: ArticleCandidate,
        evidence: ArticleEvidence,
        request: DiscoveryRequest,
    ) -> None:
        candidate.evidence = evidence
        candidate.hydration_attempt = None
        candidate.verification_status = VerificationStatus.VERIFIED
        self._mark_repost_if_needed(candidate, request)
        self._recalibrate_confidence(candidate)

    def _hydrate_browser(
        self,
        browser_evidence: EvidenceReader,
        candidate: ArticleCandidate,
        request: DiscoveryRequest,
    ) -> WxcliError | None:
        try:
            evidence = browser_evidence.get(
                str(candidate.fetch_url), request.expected_accounts
            )
            candidate.evidence = evidence
            candidate.hydration_attempt = None
            candidate.verification_status = VerificationStatus.VERIFIED
            self._mark_repost_if_needed(candidate, request)
            self._recalibrate_confidence(candidate)
            return None
        except WxcliError as error:
            self._record_browser_failure(candidate, error)
            return error
        except Exception:
            unexpected_error = WxcliError(
                ErrorCode.CHROME_ERROR,
                "The browser evidence pipeline failed unexpectedly.",
            )
            self._record_browser_failure(candidate, unexpected_error)
            return unexpected_error

    def _record_browser_failure(
        self, candidate: ArticleCandidate, error: WxcliError
    ) -> None:
        status = (
            VerificationStatus.VERIFICATION_REQUIRED
            if error.code in {
                ErrorCode.VERIFICATION_REQUIRED,
                ErrorCode.CHROME_ERROR,
                ErrorCode.BROWSER_BUSY,
            }
            else _verification_status(error.code)
        )
        stage = error.details.get("verification_stage")
        action = error.details.get("required_action")
        candidate.evidence = None
        candidate.verification_status = status
        candidate.hydration_attempt = HydrationAttempt(
            provider=Provider.CHROME,
            attempted_at=self._now(),
            verification_status=status,
            error_code=error.code,
            message=redact_text(error.message),
            verification_stage="browser" if stage == "browser" else None,
            required_action="run_browser_login" if action == "run_browser_login" else None,
        )

    def _record_remaining_browser_failures(
        self, candidates: list[ArticleCandidate], error: WxcliError
    ) -> None:
        for candidate in candidates:
            if candidate.verification_status == VerificationStatus.VERIFIED:
                continue
            completed_attempt = candidate.hydration_attempt
            if (
                completed_attempt is not None
                and completed_attempt.provider == Provider.CHROME
            ):
                continue
            self._record_browser_failure(candidate, error)

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


@contextmanager
def _evidence_batch(
    service: EvidenceReader, timeout_seconds: float
) -> Iterator[EvidenceReader]:
    batch = getattr(service, "batch", None)
    if not callable(batch):
        yield service
        return
    manager = cast(
        AbstractContextManager[EvidenceReader],
        batch(timeout_seconds=timeout_seconds),
    )
    with manager as reader:
        yield reader
