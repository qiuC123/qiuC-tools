"""Deterministic, explainable candidate ranking and hydration selection."""

from __future__ import annotations

import re
from datetime import date

from wxcli.discovery.models import (
    ArticleCandidate,
    CandidateConfidence,
    DiscoveryRequest,
    HydrationDecision,
    SearchHit,
)


def rank_candidates(
    candidates: list[ArticleCandidate], request: DiscoveryRequest
) -> list[ArticleCandidate]:
    for candidate in candidates:
        candidate.match_reasons = match_reasons(candidate, request)
        positive = len(candidate.match_reasons)
        strong_account = "expected_account_hint" in candidate.match_reasons
        strong_title = any(
            reason in candidate.match_reasons for reason in ("company_title", "query_title")
        )
        if strong_account and strong_title:
            candidate.confidence = CandidateConfidence.HIGH
        elif positive >= 2:
            candidate.confidence = CandidateConfidence.MEDIUM
        else:
            candidate.confidence = CandidateConfidence.LOW
    return sorted(candidates, key=lambda item: _candidate_sort_key(item, request))


def rank_search_hits(
    hits: list[SearchHit], request: DiscoveryRequest
) -> list[SearchHit]:
    """Rank a complete provider page before applying the public candidate limit."""

    return sorted(
        hits,
        key=lambda item: _hint_sort_key(
            title=item.title,
            snippet=item.snippet,
            account_hint=item.account_hint,
            backend_date_hint=item.backend_date_hint,
            provider_rank=item.rank,
            stable_identity=item.result_id,
            request=request,
        ),
    )


def choose_hydration(candidates: list[ArticleCandidate], request: DiscoveryRequest) -> None:
    if not request.hydrate:
        return
    selected = 0
    selected_sources: set[str] = set()
    for index, candidate in enumerate(candidates):
        if selected >= request.max_hydrate:
            break
        if index < request.priority_hydrate:
            candidate.hydration_decision = HydrationDecision.PRIORITY
            candidate.hydration_decision_reasons = ["ranked_priority"]
            selected += 1
            if source := _source_key(candidate):
                selected_sources.add(source)

    uncertainty_slots = 2
    for candidate in candidates:
        if selected >= request.max_hydrate:
            break
        if candidate.hydration_decision != HydrationDecision.CANDIDATE_ONLY:
            continue
        strong_reasons = {
            "expected_account_hint",
            "company_title",
            "query_title",
            "backend_date_hint",
        }.intersection(candidate.match_reasons)
        source = _source_key(candidate)
        if source and source not in selected_sources:
            strong_reasons.add("source_diversity")
        if strong_reasons:
            candidate.hydration_decision = HydrationDecision.SELECTED
            candidate.hydration_decision_reasons = sorted(strong_reasons)
            selected += 1
            if source:
                selected_sources.add(source)
        elif uncertainty_slots > 0:
            candidate.hydration_decision = HydrationDecision.SELECTED
            candidate.hydration_decision_reasons = ["uncertainty_sample"]
            selected += 1
            uncertainty_slots -= 1


def match_reasons(candidate: ArticleCandidate, request: DiscoveryRequest) -> list[str]:
    title = _normalize(candidate.title_hint)
    snippet = _normalize(candidate.snippet)
    account_hint = _normalize(candidate.account_hint)
    reasons: list[str] = []
    expected_names = [
        _normalize(name)
        for account in request.expected_accounts
        for name in account.display_names
    ]
    if _account_hint_quality(account_hint, expected_names) > 0:
        reasons.append("expected_account_hint")
    companies = [_normalize(value) for value in request.companies]
    if title and any(value and value in title for value in companies):
        reasons.append("company_title")
    query_terms = _query_terms(request.query)
    if title and any(term in title for term in query_terms):
        reasons.append("query_title")
    if snippet and any(term in snippet for term in query_terms):
        reasons.append("query_snippet")
    if candidate.backend_date_hint and _date_matches(candidate.backend_date_hint, request):
        reasons.append("backend_date_hint")
    return reasons


def _candidate_sort_key(
    candidate: ArticleCandidate, request: DiscoveryRequest
) -> tuple[int, int, int, int, int, int, int, str]:
    return _hint_sort_key(
        title=candidate.title_hint,
        snippet=candidate.snippet,
        account_hint=candidate.account_hint,
        backend_date_hint=candidate.backend_date_hint,
        provider_rank=candidate.search_provenance.rank,
        stable_identity=candidate.article_identity,
        request=request,
    )


def _hint_sort_key(
    *,
    title: str | None,
    snippet: str | None,
    account_hint: str | None,
    backend_date_hint: date | None,
    provider_rank: int,
    stable_identity: str,
    request: DiscoveryRequest,
) -> tuple[int, int, int, int, int, int, int, str]:
    normalized_title = _normalize(title)
    normalized_snippet = _normalize(snippet)
    normalized_account = _normalize(account_hint)
    expected_names = [
        _normalize(name)
        for account in request.expected_accounts
        for name in account.display_names
    ]
    account_quality = _account_hint_quality(normalized_account, expected_names)
    companies = [_normalize(value) for value in request.companies]
    company_title = bool(
        normalized_title
        and any(value and value in normalized_title for value in companies)
    )
    query_terms = _query_terms(request.query)
    title_matches = sum(term in normalized_title for term in query_terms)
    snippet_matches = sum(term in normalized_snippet for term in query_terms)
    account_query_match = account_quality > 0 and title_matches > 0
    date_matches = bool(backend_date_hint and _date_matches(backend_date_hint, request))
    return (
        -int(account_query_match),
        -account_quality,
        -int(company_title),
        -title_matches,
        -snippet_matches,
        -int(date_matches),
        provider_rank,
        stable_identity,
    )


def _account_hint_quality(account_hint: str, expected_names: list[str]) -> int:
    if not expected_names:
        return 0
    if not account_hint:
        return 0
    if account_hint in expected_names:
        return 2
    if any(value and value in account_hint for value in expected_names):
        return 1
    return -1


def _query_terms(value: str) -> list[str]:
    normalized = _normalize(value)
    terms = [_normalize(item) for item in re.split(r"\s+", value) if item.strip()]
    numeric_terms = re.findall(r"(?<!\d)\d{2,4}(?!\d)", normalized)
    return list(dict.fromkeys([normalized, *terms, *numeric_terms]))


def _normalize(value: str | None) -> str:
    return re.sub(r"\s+", "", value or "").casefold()


def _date_matches(value: date, request: DiscoveryRequest) -> bool:
    if request.published_after and value < request.published_after:
        return False
    if request.published_before and value > request.published_before:
        return False
    return True


def _source_key(candidate: ArticleCandidate) -> str | None:
    if candidate.article_identity.startswith("message:"):
        return ":".join(candidate.article_identity.split(":", 2)[:2])
    account = _normalize(candidate.account_hint)
    return f"account:{account}" if account else None
