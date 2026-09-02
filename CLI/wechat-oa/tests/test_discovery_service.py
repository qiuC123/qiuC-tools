from datetime import UTC, datetime
from contextlib import contextmanager

from wxcli.discovery.models import (
    CandidateConfidence,
    DiscoveryRequest,
    SearchHit,
    SearchPage,
    VerificationStatus,
)
from wxcli.discovery.service import DiscoveryService
from wxcli.discovery.store import DiscoveryStore
from wxcli.errors import ErrorCode, NotFoundError, VerificationRequiredError, WxcliError
from wxcli.evidence import EvidenceService, ExpectedAccount, IdentityStatus, build_article_evidence
from wxcli.models import Provider
from wxcli.public_article import PublicArticleParser


NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)


class FakeDiscoveryProvider:
    def __init__(
        self,
        pages: dict[int, SearchPage],
        *,
        name: str = "brave",
        page_size: int = 20,
    ) -> None:
        self.pages = pages
        self.name = name
        self.page_size = page_size
        self.calls: list[int] = []

    def search_page(self, request: DiscoveryRequest, *, offset: int, count: int) -> SearchPage:
        self.calls.append(offset)
        return self.pages.get(offset, SearchPage(hits=[], has_more=False))


class FakeEvidence:
    def __init__(self, outcomes: dict[str, object]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    def get(self, url: str, expected_accounts: list[ExpectedAccount]) -> object:
        self.calls.append(url)
        outcome = self.outcomes[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class BatchFakeEvidence(FakeEvidence):
    def __init__(self, outcomes: dict[str, object]) -> None:
        super().__init__(outcomes)
        self.batch_count = 0
        self.timeouts: list[float] = []

    @contextmanager
    def batch(self, *, timeout_seconds: float):
        self.batch_count += 1
        self.timeouts.append(timeout_seconds)
        yield self


def evidence(url: str, *, account: str = "Acme Jobs", biz: str = "BIZ", date: bool = True):
    published = "var ct='1785542400';" if date else ""
    html = f"""
    <h1 id="activity-name">Acme campus hiring</h1>
    <a id="js_name">{account}</a>
    <div id="js_content"><p>Apply now</p></div>
    <script>var __biz='{biz}';{published}</script>
    """
    document = PublicArticleParser.parse(html, url, Provider.HTTP)
    return build_article_evidence(
        document,
        [ExpectedAccount(biz_id="BIZ", display_names=["Acme Jobs"])],
        now=NOW,
    )


def hit(rank: int, url: str, title: str = "Acme 2027 校园招聘") -> SearchHit:
    return SearchHit(title=title, url=url, rank=rank, result_id=f"r{rank}")


def test_search_validates_deduplicates_paginates_caches_and_returns_cursor(tmp_path) -> None:
    first_page = SearchPage(
        hits=[
            hit(1, "https://example.com/wrapper"),
            hit(2, "https://mp.weixin.qq.com/s/T1"),
            hit(3, "https://mp.weixin.qq.com/s/T1"),
        ],
        has_more=True,
        next_offset=1,
    )
    second_page = SearchPage(
        hits=[hit(4, "https://mp.weixin.qq.com/s/T2")], has_more=True, next_offset=2
    )
    provider = FakeDiscoveryProvider({0: first_page, 1: second_page})
    store = DiscoveryStore(tmp_path / "state.sqlite3")
    service = DiscoveryService(provider, store, now=lambda: NOW)

    result = service.search(DiscoveryRequest(query="2027 校园招聘", limit=2))

    assert [item.article_identity for item in result.candidates] == ["token:T1", "token:T2"]
    assert result.summary.received == 4
    assert result.summary.duplicates_removed == 1
    assert result.next_cursor is not None
    assert provider.calls == [0, 1]

    provider.calls.clear()
    cached = service.search(DiscoveryRequest(query="2027 校园招聘", limit=2))
    assert len(cached.candidates) == 2
    assert provider.calls == []


def test_provider_page_size_and_name_drive_cached_cursor_continuation(tmp_path) -> None:
    page = SearchPage(
        hits=[
            hit(index, f"https://mp.weixin.qq.com/s/T{index}")
            for index in range(1, 61)
        ],
        has_more=False,
    )
    provider = FakeDiscoveryProvider({0: page}, name="exa", page_size=100)
    service = DiscoveryService(
        provider,
        DiscoveryStore(tmp_path / "state.sqlite3"),
        now=lambda: NOW,
    )

    first = service.search(DiscoveryRequest(query="campus", limit=50))
    assert first.search_provider == "exa"
    assert len(first.candidates) == 50
    assert first.next_cursor is not None
    assert {item.search_provenance.provider for item in first.candidates} == {"exa"}
    assert first.candidates[0].search_provenance.rank == 1
    assert first.candidates[0].search_provenance.result_id == "r1"
    assert provider.calls == [0]

    provider.calls.clear()
    second = service.search(
        DiscoveryRequest(query="campus", limit=50, cursor=first.next_cursor)
    )
    assert len(second.candidates) == 10
    assert second.next_cursor is None
    assert provider.calls == []


def test_exa_candidates_still_require_strict_wechat_article_urls(tmp_path) -> None:
    provider = FakeDiscoveryProvider(
        {
            0: SearchPage(
                hits=[
                    hit(1, "https://mp.weixin.qq.com/s/VALID"),
                    hit(2, "https://mp.weixin.qq.com/not-an-article"),
                    hit(3, "https://example.com/s/OTHER-HOST"),
                    hit(4, "https://user@mp.weixin.qq.com/s/CREDENTIALS"),
                ],
                has_more=False,
            )
        },
        name="exa",
        page_size=100,
    )
    service = DiscoveryService(
        provider,
        DiscoveryStore(tmp_path / "state.sqlite3"),
        now=lambda: NOW,
    )

    result = service.search(DiscoveryRequest(query="campus"))

    assert result.search_provider == "exa"
    assert result.summary.received == 4
    assert result.summary.accepted == 1
    assert [item.fetch_url.encoded_string() for item in result.candidates] == [
        "https://mp.weixin.qq.com/s/VALID"
    ]


def test_invalid_provider_page_size_is_rejected_before_search(tmp_path) -> None:
    provider = FakeDiscoveryProvider({}, page_size=0)

    with __import__("pytest").raises(WxcliError) as raised:
        DiscoveryService(provider, DiscoveryStore(tmp_path / "state.sqlite3"))

    assert raised.value.code == ErrorCode.LOCAL_CONFIGURATION_ERROR


def test_new_only_and_checkpoint_are_incremental(tmp_path) -> None:
    provider = FakeDiscoveryProvider(
        {0: SearchPage(hits=[hit(1, "https://mp.weixin.qq.com/s/T1")], has_more=False)}
    )
    service = DiscoveryService(provider, DiscoveryStore(tmp_path / "state.sqlite3"), now=lambda: NOW)
    first = service.search(DiscoveryRequest(query="x"))
    second = service.search(DiscoveryRequest(query="x", new_only=True))
    checkpointed = service.search(DiscoveryRequest(query="x", checkpoint=first.checkpoint))

    assert len(first.candidates) == 1
    assert second.candidates == []
    assert checkpointed.candidates == []


def test_hydration_partial_success_and_browser_requires_explicit_authorization(tmp_path) -> None:
    url1 = "https://mp.weixin.qq.com/s/T1"
    url2 = "https://mp.weixin.qq.com/s/T2"
    url3 = "https://mp.weixin.qq.com/s/T3"
    provider = FakeDiscoveryProvider(
        {
            0: SearchPage(
                hits=[hit(1, url1), hit(2, url2), hit(3, url3)], has_more=False
            )
        }
    )
    http = FakeEvidence(
        {
            url1: evidence(url1),
            url2: VerificationRequiredError(),
            url3: NotFoundError("missing"),
        }
    )
    browser = FakeEvidence({url2: evidence(url2)})
    service = DiscoveryService(
        provider,
        DiscoveryStore(tmp_path / "state.sqlite3"),
        http_evidence=http,  # type: ignore[arg-type]
        browser_evidence=browser,  # type: ignore[arg-type]
        now=lambda: NOW,
    )

    without_browser = service.search(
        DiscoveryRequest(query="x", hydrate=True, priority_hydrate=3, max_hydrate=3)
    )
    assert browser.calls == []
    assert without_browser.summary.partial is True
    assert [item.verification_status for item in without_browser.candidates] == [
        VerificationStatus.VERIFIED,
        VerificationStatus.VERIFICATION_REQUIRED,
        VerificationStatus.NOT_FOUND,
    ]

    with_browser = service.search(
        DiscoveryRequest(
            query="x", hydrate=True, priority_hydrate=3, max_hydrate=3, allow_browser=True
        )
    )
    assert browser.calls == [url2]
    assert with_browser.summary.verified == 2
    assert with_browser.summary.partial is True


def test_network_retries_once_and_chrome_failure_preserves_verification_status(tmp_path) -> None:
    url = "https://mp.weixin.qq.com/s/T1"

    class NetworkThenVerification(FakeEvidence):
        def get(self, url: str, expected_accounts: list[ExpectedAccount]) -> object:
            self.calls.append(url)
            if len(self.calls) == 1:
                raise WxcliError(ErrorCode.NETWORK_ERROR, "offline")
            raise VerificationRequiredError()

    http = NetworkThenVerification({})
    browser = FakeEvidence({url: WxcliError(ErrorCode.CHROME_ERROR, "chrome unavailable")})
    service = DiscoveryService(
        FakeDiscoveryProvider({0: SearchPage(hits=[hit(1, url)], has_more=False)}),
        DiscoveryStore(tmp_path / "state.sqlite3"),
        http_evidence=http,  # type: ignore[arg-type]
        browser_evidence=browser,  # type: ignore[arg-type]
        now=lambda: NOW,
    )
    result = service.search(
        DiscoveryRequest(
            query="x", hydrate=True, priority_hydrate=1, max_hydrate=1, allow_browser=True
        )
    )

    candidate = result.candidates[0]
    assert len(http.calls) == 2
    assert candidate.verification_status == VerificationStatus.VERIFICATION_REQUIRED
    assert candidate.hydration_attempt is not None
    assert candidate.hydration_attempt.error_code == ErrorCode.CHROME_ERROR


def test_browser_batch_opens_once_and_uses_fresh_candidate_reads(tmp_path) -> None:
    urls = [f"https://mp.weixin.qq.com/s/T{index}" for index in range(1, 3)]
    provider = FakeDiscoveryProvider(
        {0: SearchPage(hits=[hit(index, url) for index, url in enumerate(urls, 1)], has_more=False)}
    )
    http = FakeEvidence({url: VerificationRequiredError() for url in urls})
    browser = BatchFakeEvidence({url: evidence(url) for url in urls})
    service = DiscoveryService(
        provider,
        DiscoveryStore(tmp_path / "state.sqlite3"),
        http_evidence=http,  # type: ignore[arg-type]
        browser_evidence=browser,  # type: ignore[arg-type]
        now=lambda: NOW,
    )

    result = service.search(
        DiscoveryRequest(
            query="x",
            hydrate=True,
            priority_hydrate=2,
            max_hydrate=2,
            allow_browser=True,
        )
    )

    assert browser.batch_count == 1
    assert browser.calls == urls
    assert result.browser_fallback is not None
    assert result.browser_fallback.eligible == 2
    assert result.browser_fallback.attempted == 2
    assert result.browser_fallback.verified == 2


def test_first_human_challenge_stops_browser_run_and_marks_unvisited(tmp_path) -> None:
    urls = [f"https://mp.weixin.qq.com/s/T{index}" for index in range(1, 4)]
    challenge = VerificationRequiredError(
        verification_stage="browser",
        required_action="run_browser_login",
    )
    browser = BatchFakeEvidence(
        {urls[0]: challenge, urls[1]: evidence(urls[1]), urls[2]: evidence(urls[2])}
    )
    service = DiscoveryService(
        FakeDiscoveryProvider(
            {0: SearchPage(hits=[hit(index, url) for index, url in enumerate(urls, 1)], has_more=False)}
        ),
        DiscoveryStore(tmp_path / "state.sqlite3"),
        http_evidence=FakeEvidence({url: VerificationRequiredError() for url in urls}),  # type: ignore[arg-type]
        browser_evidence=browser,  # type: ignore[arg-type]
        now=lambda: NOW,
    )

    result = service.search(
        DiscoveryRequest(
            query="x",
            hydrate=True,
            priority_hydrate=3,
            max_hydrate=3,
            allow_browser=True,
        )
    )

    assert browser.calls == [urls[0]]
    assert result.browser_fallback is not None
    assert result.browser_fallback.attempted == 1
    assert result.browser_fallback.user_action_required == 3
    assert all(
        candidate.hydration_attempt is not None
        and candidate.hydration_attempt.required_action == "run_browser_login"
        for candidate in result.candidates
    )


def test_chrome_crash_does_not_restart_or_discard_completed_http_results(tmp_path) -> None:
    urls = ["https://mp.weixin.qq.com/s/T1", "https://mp.weixin.qq.com/s/T2"]
    browser = BatchFakeEvidence(
        {urls[0]: WxcliError(ErrorCode.CHROME_ERROR, "crashed"), urls[1]: evidence(urls[1])}
    )
    service = DiscoveryService(
        FakeDiscoveryProvider(
            {0: SearchPage(hits=[hit(1, urls[0]), hit(2, urls[1])], has_more=False)}
        ),
        DiscoveryStore(tmp_path / "state.sqlite3"),
        http_evidence=FakeEvidence({url: VerificationRequiredError() for url in urls}),  # type: ignore[arg-type]
        browser_evidence=browser,  # type: ignore[arg-type]
        now=lambda: NOW,
    )

    result = service.search(
        DiscoveryRequest(
            query="x",
            hydrate=True,
            priority_hydrate=2,
            max_hydrate=2,
            allow_browser=True,
        )
    )

    assert browser.batch_count == 1
    assert browser.calls == [urls[0]]
    assert result.browser_fallback is not None
    assert result.browser_fallback.attempted == 1
    assert all(
        candidate.hydration_attempt is not None
        and candidate.hydration_attempt.error_code == ErrorCode.CHROME_ERROR
        for candidate in result.candidates
    )


def test_browser_not_found_continues_but_busy_marks_all_eligible(tmp_path) -> None:
    urls = ["https://mp.weixin.qq.com/s/T1", "https://mp.weixin.qq.com/s/T2"]
    pages = {0: SearchPage(hits=[hit(1, urls[0]), hit(2, urls[1])], has_more=False)}
    http = FakeEvidence({url: VerificationRequiredError() for url in urls})
    browser = BatchFakeEvidence({urls[0]: NotFoundError("missing"), urls[1]: evidence(urls[1])})
    service = DiscoveryService(
        FakeDiscoveryProvider(pages),
        DiscoveryStore(tmp_path / "continue.sqlite3"),
        http_evidence=http,  # type: ignore[arg-type]
        browser_evidence=browser,  # type: ignore[arg-type]
        now=lambda: NOW,
    )
    request = DiscoveryRequest(
        query="x", hydrate=True, priority_hydrate=2, max_hydrate=2, allow_browser=True
    )

    continued = service.search(request)

    assert browser.calls == urls
    assert continued.candidates[0].verification_status is VerificationStatus.NOT_FOUND
    assert continued.candidates[1].verification_status is VerificationStatus.VERIFIED

    class BusyBrowser(FakeEvidence):
        @contextmanager
        def batch(self, *, timeout_seconds: float):
            raise WxcliError(ErrorCode.BROWSER_BUSY, "busy")
            yield self

    busy = DiscoveryService(
        FakeDiscoveryProvider(pages),
        DiscoveryStore(tmp_path / "busy.sqlite3"),
        http_evidence=FakeEvidence({url: VerificationRequiredError() for url in urls}),  # type: ignore[arg-type]
        browser_evidence=BusyBrowser({}),  # type: ignore[arg-type]
        now=lambda: NOW,
    ).search(request)
    assert busy.browser_fallback is not None
    assert busy.browser_fallback.attempted == 0
    assert all(
        item.hydration_attempt is not None
        and item.hydration_attempt.error_code is ErrorCode.BROWSER_BUSY
        for item in busy.candidates
    )


def test_browser_close_failure_preserves_completed_browser_outcomes(tmp_path) -> None:
    class CloseFailureBrowser(BatchFakeEvidence):
        @contextmanager
        def batch(self, *, timeout_seconds: float):
            self.batch_count += 1
            self.timeouts.append(timeout_seconds)
            yield self
            raise WxcliError(ErrorCode.CHROME_ERROR, "close failed")

    urls = ["https://mp.weixin.qq.com/s/T1", "https://mp.weixin.qq.com/s/T2"]
    browser = CloseFailureBrowser(
        {urls[0]: NotFoundError("missing"), urls[1]: evidence(urls[1])}
    )
    result = DiscoveryService(
        FakeDiscoveryProvider(
            {0: SearchPage(hits=[hit(1, urls[0]), hit(2, urls[1])], has_more=False)}
        ),
        DiscoveryStore(tmp_path / "close.sqlite3"),
        http_evidence=FakeEvidence(
            {url: VerificationRequiredError() for url in urls}
        ),  # type: ignore[arg-type]
        browser_evidence=browser,  # type: ignore[arg-type]
        now=lambda: NOW,
    ).search(
        DiscoveryRequest(
            query="x",
            hydrate=True,
            priority_hydrate=2,
            max_hydrate=2,
            allow_browser=True,
        )
    )

    assert result.candidates[0].verification_status is VerificationStatus.NOT_FOUND
    assert result.candidates[0].hydration_attempt is not None
    assert result.candidates[0].hydration_attempt.error_code is ErrorCode.NOT_FOUND
    assert result.candidates[1].verification_status is VerificationStatus.VERIFIED
    assert result.candidates[1].evidence is not None


def test_browser_phase_respects_remaining_total_deadline(tmp_path) -> None:
    url = "https://mp.weixin.qq.com/s/T1"
    ticks = iter((0.0, 0.0, 601.0))
    service = DiscoveryService(
        FakeDiscoveryProvider({0: SearchPage(hits=[hit(1, url)], has_more=False)}),
        DiscoveryStore(tmp_path / "deadline.sqlite3"),
        http_evidence=FakeEvidence({url: VerificationRequiredError()}),  # type: ignore[arg-type]
        browser_evidence=BatchFakeEvidence({url: evidence(url)}),  # type: ignore[arg-type]
        now=lambda: NOW,
        monotonic=lambda: next(ticks),
    )

    result = service.search(
        DiscoveryRequest(
            query="x", hydrate=True, priority_hydrate=1, max_hydrate=1, allow_browser=True
        )
    )

    candidate = result.candidates[0]
    assert candidate.hydration_attempt is not None
    assert candidate.hydration_attempt.error_code is ErrorCode.CHROME_ERROR
    assert result.browser_fallback is not None
    assert result.browser_fallback.attempted == 0


def test_strict_identity_and_date_filters_use_only_hydrated_source(tmp_path) -> None:
    matching_url = "https://mp.weixin.qq.com/s/T1"
    mismatch_url = "https://mp.weixin.qq.com/s/T2"
    undated_url = "https://mp.weixin.qq.com/s/T3"
    outcomes = {
        matching_url: evidence(matching_url),
        mismatch_url: evidence(mismatch_url, account="Other", biz="OTHER"),
        undated_url: evidence(undated_url, date=False),
    }
    service = DiscoveryService(
        FakeDiscoveryProvider(
            {
                0: SearchPage(
                    hits=[hit(1, matching_url), hit(2, mismatch_url), hit(3, undated_url)],
                    has_more=False,
                )
            }
        ),
        DiscoveryStore(tmp_path / "state.sqlite3"),
        http_evidence=FakeEvidence(outcomes),  # type: ignore[arg-type]
        now=lambda: NOW,
    )
    result = service.search(
        DiscoveryRequest(
            query="x",
            expected_accounts=[ExpectedAccount(biz_id="BIZ", display_names=["Acme Jobs"])],
            hydrate=True,
            priority_hydrate=3,
            max_hydrate=3,
            require_account_match=True,
            require_published_date=True,
        )
    )

    assert [str(item.fetch_url) for item in result.candidates] == [matching_url]
    assert result.candidates[0].evidence is not None
    assert result.candidates[0].evidence.account_identity.status == IdentityStatus.ALLOWLIST_MATCHED
    assert result.summary.hydration_attempted == 3


def test_missing_evidence_service_and_parse_failure_are_safe(tmp_path) -> None:
    url = "https://mp.weixin.qq.com/s/T1"
    provider = FakeDiscoveryProvider({0: SearchPage(hits=[hit(1, url)], has_more=False)})
    with __import__("pytest").raises(WxcliError) as missing:
        DiscoveryService(provider, DiscoveryStore(tmp_path / "missing.sqlite3"), now=lambda: NOW).search(
            DiscoveryRequest(query="x", hydrate=True, priority_hydrate=1, max_hydrate=1)
        )
    assert missing.value.code == ErrorCode.LOCAL_CONFIGURATION_ERROR

    service = DiscoveryService(
        provider,
        DiscoveryStore(tmp_path / "parse.sqlite3"),
        http_evidence=FakeEvidence({url: WxcliError(ErrorCode.PARSING_ERROR, "bad")}),  # type: ignore[arg-type]
        now=lambda: NOW,
    )
    result = service.search(
        DiscoveryRequest(query="x", hydrate=True, priority_hydrate=1, max_hydrate=1)
    )
    assert result.candidates[0].verification_status == VerificationStatus.PARSE_FAILED


def test_unexpected_evidence_exception_is_a_safe_partial_failure(tmp_path) -> None:
    url = "https://mp.weixin.qq.com/s/T1"
    service = DiscoveryService(
        FakeDiscoveryProvider({0: SearchPage(hits=[hit(1, url)], has_more=False)}),
        DiscoveryStore(tmp_path / "state.sqlite3"),
        http_evidence=FakeEvidence({url: ValueError("secret parser detail")}),  # type: ignore[arg-type]
        now=lambda: NOW,
    )

    result = service.search(
        DiscoveryRequest(query="x", hydrate=True, priority_hydrate=1, max_hydrate=1)
    )

    candidate = result.candidates[0]
    assert result.summary.partial is True
    assert candidate.verification_status == VerificationStatus.PARSE_FAILED
    assert candidate.hydration_attempt is not None
    assert candidate.hydration_attempt.error_code == ErrorCode.PARSING_ERROR
    assert "secret parser detail" not in candidate.hydration_attempt.message


def test_unexpected_browser_exception_preserves_source_verification_state(tmp_path) -> None:
    url = "https://mp.weixin.qq.com/s/T1"
    service = DiscoveryService(
        FakeDiscoveryProvider({0: SearchPage(hits=[hit(1, url)], has_more=False)}),
        DiscoveryStore(tmp_path / "state.sqlite3"),
        http_evidence=FakeEvidence({url: VerificationRequiredError()}),  # type: ignore[arg-type]
        browser_evidence=FakeEvidence({url: RuntimeError("browser secret")}),  # type: ignore[arg-type]
        now=lambda: NOW,
    )

    result = service.search(
        DiscoveryRequest(
            query="x", hydrate=True, priority_hydrate=1, max_hydrate=1, allow_browser=True
        )
    )

    candidate = result.candidates[0]
    assert candidate.verification_status == VerificationStatus.VERIFICATION_REQUIRED
    assert candidate.hydration_attempt is not None
    assert candidate.hydration_attempt.error_code == ErrorCode.CHROME_ERROR
    assert "browser secret" not in candidate.hydration_attempt.message


def test_repost_suspicion_and_name_only_match_never_become_high_confidence(tmp_path) -> None:
    repost_url = "https://mp.weixin.qq.com/s/T1"
    name_url = "https://mp.weixin.qq.com/s/T2"
    repost_hit = hit(1, repost_url)
    repost_hit.account_hint = "Acme Jobs"

    repost_evidence = evidence(repost_url, account="Other", biz="OTHER")
    original_repost_hash = repost_evidence.evidence_sha256
    name_html = """
      <h1 id="activity-name">Hiring</h1><a id="js_name">Acme Jobs</a>
      <div id="js_content"><p>Text</p></div>
    """
    name_document = PublicArticleParser.parse(name_html, name_url, Provider.HTTP)
    name_evidence = build_article_evidence(
        name_document, [ExpectedAccount(display_names=["Acme Jobs"])], now=NOW
    )
    service = DiscoveryService(
        FakeDiscoveryProvider(
            {0: SearchPage(hits=[repost_hit, hit(2, name_url)], has_more=False)}
        ),
        DiscoveryStore(tmp_path / "state.sqlite3"),
        http_evidence=FakeEvidence({repost_url: repost_evidence, name_url: name_evidence}),  # type: ignore[arg-type]
        now=lambda: NOW,
    )
    result = service.search(
        DiscoveryRequest(
            query="x",
            expected_accounts=[ExpectedAccount(display_names=["Acme Jobs"])],
            hydrate=True,
            priority_hydrate=2,
            max_hydrate=2,
        )
    )

    assert result.candidates[0].evidence is not None
    assert result.candidates[0].evidence.account_identity.status == IdentityStatus.REPOST_SUSPECTED
    assert result.candidates[0].evidence.evidence_sha256 != original_repost_hash
    assert result.candidates[0].confidence == CandidateConfidence.LOW
    assert result.candidates[1].confidence == CandidateConfidence.MEDIUM


def test_strict_filters_remove_failed_and_out_of_range_candidates(tmp_path) -> None:
    failed_url = "https://mp.weixin.qq.com/s/T1"
    dated_url = "https://mp.weixin.qq.com/s/T2"
    service = DiscoveryService(
        FakeDiscoveryProvider(
            {0: SearchPage(hits=[hit(1, failed_url), hit(2, dated_url)], has_more=False)}
        ),
        DiscoveryStore(tmp_path / "state.sqlite3"),
        http_evidence=FakeEvidence(
            {
                failed_url: VerificationRequiredError(),
                dated_url: evidence(dated_url),
            }
        ),  # type: ignore[arg-type]
        now=lambda: NOW,
    )
    result = service.search(
        DiscoveryRequest(
            query="x",
            published_before="2025-01-01",
            hydrate=True,
            priority_hydrate=2,
            max_hydrate=2,
            require_published_date=True,
        )
    )
    assert result.candidates == []
    assert result.summary.partial is True
    assert result.summary.hydration_attempted == 2


def test_batch_deadline_marks_unfinished_candidates(monkeypatch, tmp_path) -> None:
    import wxcli.discovery.hydration as hydration_module

    class DummyFuture:
        def done(self) -> bool:
            return False

        def cancel(self) -> bool:
            return True

    class DummyExecutor:
        def __init__(self, **kwargs: object) -> None:
            self.future = DummyFuture()

        def submit(self, function, *args):
            return self.future

        def shutdown(self, **kwargs: object) -> None:
            return None

    def timeout(*args, **kwargs):
        raise hydration_module.FuturesTimeoutError

    monkeypatch.setattr(hydration_module, "ThreadPoolExecutor", DummyExecutor)
    monkeypatch.setattr(hydration_module, "as_completed", timeout)
    url = "https://mp.weixin.qq.com/s/T1"
    service = DiscoveryService(
        FakeDiscoveryProvider({0: SearchPage(hits=[hit(1, url)], has_more=False)}),
        DiscoveryStore(tmp_path / "state.sqlite3"),
        http_evidence=FakeEvidence({}),  # type: ignore[arg-type]
        now=lambda: NOW,
    )
    result = service.search(
        DiscoveryRequest(query="x", hydrate=True, priority_hydrate=1, max_hydrate=1)
    )
    assert result.candidates[0].verification_status == VerificationStatus.NETWORK_FAILED
