from datetime import UTC, date, datetime

import pytest

from wxcli.discovery.identity import _one, article_identity, query_fingerprint
from wxcli.discovery.models import (
    ArticleCandidate,
    CandidateConfidence,
    DiscoveryRequest,
    HydrationDecision,
    SearchProvenance,
)
from wxcli.discovery.ranking import choose_hydration, rank_candidates
from wxcli.discovery.tokens import (
    decode_checkpoint,
    decode_cursor,
    encode_checkpoint,
    encode_cursor,
)
from wxcli.discovery import tokens
from wxcli.errors import ValidationError
from wxcli.evidence import ExpectedAccount


def request(**overrides: object) -> DiscoveryRequest:
    values: dict[str, object] = {"query": "2027 校园招聘"}
    values.update(overrides)
    return DiscoveryRequest.model_validate(values)


@pytest.mark.parametrize(
    ("url", "identity"),
    [
        ("https://mp.weixin.qq.com/s/TOKEN", "token:TOKEN"),
        (
            "https://mp.weixin.qq.com/s?__biz=BIZ&mid=123&idx=2&scene=21",
            "message:BIZ:123:2",
        ),
        ("https://mp.weixin.qq.com/s?__biz=BIZ&mid=123", "message:BIZ:123:1"),
    ],
)
def test_article_identity_accepts_only_strict_direct_urls(url: str, identity: str) -> None:
    normalized, actual = article_identity(url)
    assert normalized.startswith("https://mp.weixin.qq.com/s")
    assert actual == identity


@pytest.mark.parametrize(
    "url",
    [
        "http://mp.weixin.qq.com/s/TOKEN",
        "https://example.com/?url=https://mp.weixin.qq.com/s/TOKEN",
        "https://mp.weixin.qq.com/not-an-article",
        "https://mp.weixin.qq.com/s/TOKEN?tracking=1",
    ],
)
def test_article_identity_rejects_non_articles_and_wrappers(url: str) -> None:
    with pytest.raises(ValidationError):
        article_identity(url)


def test_request_forbids_unknown_fields_and_invalid_controls() -> None:
    with pytest.raises(ValueError):
        DiscoveryRequest.model_validate({"query": "x", "api_key": "secret"})
    with pytest.raises(ValueError):
        request(published_after="2027-02-01", published_before="2027-01-01")
    with pytest.raises(ValueError):
        request(priority_hydrate=11, max_hydrate=10)
    with pytest.raises(ValueError):
        request(allow_browser=True)
    with pytest.raises(ValueError):
        request(require_account_match=True)
    with pytest.raises(ValueError):
        request(cursor="x", checkpoint="y")
    with pytest.raises(ValueError):
        request(query="   ")
    with pytest.raises(ValueError):
        request(query="campus\nsite:example.com")
    with pytest.raises(ValueError):
        request(companies=["x" * 201])
    with pytest.raises(ValueError):
        request(companies=[f"{index:02d}" + "x" * 198 for index in range(10)])
    with pytest.raises(ValueError):
        request(cursor="x" * 4097)
    assert request(companies=[" Acme ", "acme", " "]).companies == ["Acme"]
    with pytest.raises(ValidationError):
        _one({}, "mid")


def test_query_fingerprint_ignores_run_controls_but_includes_query() -> None:
    first = query_fingerprint(request(limit=5, hydrate=False))
    second = query_fingerprint(request(limit=50, hydrate=True))
    different = query_fingerprint(request(query="different"))

    assert first == second
    assert first != different


def test_cursor_and_checkpoint_are_bound_to_provider_and_query() -> None:
    fingerprint = "a" * 64
    cursor = encode_cursor("brave", fingerprint, 2)
    checkpoint_time = datetime(2026, 8, 1, tzinfo=UTC)
    checkpoint = encode_checkpoint("brave", fingerprint, checkpoint_time)

    assert decode_cursor(cursor, "brave", fingerprint) == 2
    assert decode_checkpoint(checkpoint, "brave", fingerprint) == checkpoint_time
    with pytest.raises(ValidationError):
        decode_cursor(cursor, "other", fingerprint)
    with pytest.raises(ValidationError):
        decode_checkpoint(checkpoint, "brave", "b" * 64)
    with pytest.raises(ValidationError):
        decode_cursor(cursor[:-2] + "xx", "brave", fingerprint)


def test_tokens_reject_wrong_kind_schema_and_payload_fields() -> None:
    fingerprint = "a" * 64
    wrong_kind = tokens._encode(
        {"schema": "1", "kind": "checkpoint", "provider": "brave", "query_fingerprint": fingerprint}
    )
    wrong_schema = tokens._encode(
        {"schema": "2", "kind": "cursor", "provider": "brave", "query_fingerprint": fingerprint, "offset": 1}
    )
    bad_offset = tokens._encode(
        {"schema": "1", "kind": "cursor", "provider": "brave", "query_fingerprint": fingerprint, "offset": True}
    )
    bad_time = tokens._encode(
        {"schema": "1", "kind": "checkpoint", "provider": "brave", "query_fingerprint": fingerprint, "observed_before": "bad"}
    )
    naive_time = tokens._encode(
        {"schema": "1", "kind": "checkpoint", "provider": "brave", "query_fingerprint": fingerprint, "observed_before": "2026-01-01T00:00:00"}
    )
    for value, decoder in (
        (wrong_kind, lambda: decode_cursor(wrong_kind, "brave", fingerprint)),
        (wrong_schema, lambda: decode_cursor(wrong_schema, "brave", fingerprint)),
        (bad_offset, lambda: decode_cursor(bad_offset, "brave", fingerprint)),
        (bad_time, lambda: decode_checkpoint(bad_time, "brave", fingerprint)),
        (naive_time, lambda: decode_checkpoint(naive_time, "brave", fingerprint)),
    ):
        assert value
        with pytest.raises(ValidationError):
            decoder()


def candidate(
    rank: int,
    *,
    title: str = "misc",
    snippet: str = "",
    account: str | None = None,
    date_hint: date | None = None,
) -> ArticleCandidate:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    return ArticleCandidate(
        fetch_url=f"https://mp.weixin.qq.com/s/T{rank}",
        article_identity=f"token:T{rank}",
        title_hint=title,
        snippet=snippet,
        account_hint=account,
        backend_date_hint=date_hint,
        discovered_at=now,
        last_seen_at=now,
        search_provenance=SearchProvenance(provider="brave", rank=rank, result_id=str(rank)),
        confidence=CandidateConfidence.LOW,
    )


def test_ranking_reasons_are_deterministic_and_high_requires_strong_account_title() -> None:
    discovery_request = request(
        companies=["Acme"],
        expected_accounts=[ExpectedAccount(display_names=["Acme Jobs"])],
        published_after="2026-01-01",
        published_before="2026-12-31",
    )
    strong = candidate(
        5,
        title="Acme 2027 校园招聘",
        account="Acme Jobs",
        date_hint=date(2026, 8, 1),
    )
    weak = candidate(1, snippet="校园招聘")

    ordered = rank_candidates([weak, strong], discovery_request)

    assert ordered[0] is strong
    assert strong.confidence == CandidateConfidence.HIGH
    assert strong.match_reasons == [
        "expected_account_hint",
        "company_title",
        "query_title",
        "backend_date_hint",
    ]
    assert weak.confidence == CandidateConfidence.LOW


def test_hydration_selects_priority_then_bounded_uncertainty() -> None:
    candidates = [candidate(index + 1) for index in range(25)]
    discovery_request = request(hydrate=True, priority_hydrate=10, max_hydrate=12)

    choose_hydration(candidates, discovery_request)

    assert sum(item.hydration_decision == HydrationDecision.PRIORITY for item in candidates) == 10
    assert sum(item.hydration_decision == HydrationDecision.SELECTED for item in candidates) == 2
    assert sum(item.hydration_decision != HydrationDecision.CANDIDATE_ONLY for item in candidates) == 12


def test_hydration_disabled_keeps_all_candidates_unattempted() -> None:
    candidates = [candidate(1, title="Acme x")]
    choose_hydration(candidates, request(companies=["Acme"]))
    assert candidates[0].hydration_decision == HydrationDecision.CANDIDATE_ONLY


def test_medium_ranking_strong_selection_and_date_misses() -> None:
    medium = candidate(1, title="Acme notice", snippet="campus", date_hint=date(2025, 1, 1))
    discovery_request = request(
        query="campus", companies=["Acme"], published_after="2026-01-01", hydrate=True,
        priority_hydrate=0, max_hydrate=1
    )
    rank_candidates([medium], discovery_request)
    choose_hydration([medium], discovery_request)
    assert medium.confidence == CandidateConfidence.MEDIUM
    assert "backend_date_hint" not in medium.match_reasons
    assert medium.hydration_decision == HydrationDecision.SELECTED
    assert medium.hydration_decision_reasons == ["company_title"]

    none = [candidate(2)]
    choose_hydration(none, request(hydrate=True, priority_hydrate=0, max_hydrate=0))
    assert none[0].hydration_decision == HydrationDecision.CANDIDATE_ONLY


def test_hydration_can_select_a_distinct_observed_source() -> None:
    first = candidate(1, account="Source A")
    second = candidate(2, account="Source B")
    discovery_request = request(hydrate=True, priority_hydrate=1, max_hydrate=2)
    choose_hydration([first, second], discovery_request)
    assert second.hydration_decision == HydrationDecision.SELECTED
    assert second.hydration_decision_reasons == ["source_diversity"]
