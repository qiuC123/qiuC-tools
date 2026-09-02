import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from wxcli.discovery.auth import (
    BRAVE_SERVICE_NAME,
    EXA_SERVICE_NAME,
    DiscoverySecretStore,
)
from wxcli.discovery.brave import BRAVE_SEARCH_URL, BraveDiscoveryProvider
from wxcli.discovery.exa import EXA_SEARCH_URL, ExaDiscoveryProvider
from wxcli.discovery.models import DiscoveryRequest, SearchHit, SearchPage
from wxcli.discovery.provider import DiscoveryFailureReason
from wxcli.discovery.service import DiscoveryService
from wxcli.discovery.store import DiscoveryStore
from wxcli.errors import ErrorCode, VerificationRequiredError, WxcliError
from wxcli.redaction import redact, redact_text


class MemoryBackend:
    def __init__(self, fail: bool = False) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.fail = fail

    def get_password(self, service_name: str, username: str) -> str | None:
        if self.fail:
            raise RuntimeError
        assert service_name in {BRAVE_SERVICE_NAME, EXA_SERVICE_NAME}
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        if self.fail:
            raise RuntimeError
        assert service_name in {BRAVE_SERVICE_NAME, EXA_SERVICE_NAME}
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.values.pop((service_name, username), None)


def test_discovery_secret_store_is_separate_validated_and_safe() -> None:
    backend = MemoryBackend()
    store = DiscoverySecretStore(backend)
    assert store.get_brave_api_key() is None
    store.set_brave_api_key("secret")
    assert store.get_brave_api_key() == "secret"
    assert store.get_api_key("exa") is None
    store.set_api_key("exa", "exa-secret")
    assert store.get_api_key("exa") == "exa-secret"
    assert store.get_brave_api_key() == "secret"
    with pytest.raises(WxcliError) as empty:
        store.set_brave_api_key(" ")
    assert empty.value.code == ErrorCode.VALIDATION_ERROR
    with pytest.raises(WxcliError) as unavailable:
        DiscoverySecretStore(MemoryBackend(fail=True)).get_brave_api_key()
    assert unavailable.value.code == ErrorCode.LOCAL_CONFIGURATION_ERROR


def test_exa_sends_bounded_domain_filtered_query_and_sanitizes_results() -> None:
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Acme hiring",
                        "url": "https://mp.weixin.qq.com/s/TOKEN",
                        "publishedDate": "2026-08-01T12:30:00.000Z",
                        "author": "Acme Jobs",
                        "id": "provider-result-id",
                        "text": "must not escape",
                    }
                ],
                "requestId": "request-id-must-not-escape",
                "costDollars": {"total": 1},
            },
        )

    provider = ExaDiscoveryProvider(
        httpx.Client(transport=httpx.MockTransport(handler)),
        "example-secret",
    )
    page = provider.search_page(
        DiscoveryRequest(
            query="2027 校园招聘",
            companies=["Acme"],
            published_after="2026-01-01",
            published_before="2026-12-31",
        ),
        offset=0,
        count=100,
    )

    assert observed[0].method == "POST"
    assert str(observed[0].url) == EXA_SEARCH_URL
    assert observed[0].headers["x-api-key"] == "example-secret"
    body = json.loads(observed[0].content)
    assert body == {
        "query": "2027 校园招聘 Acme",
        "includeDomains": ["mp.weixin.qq.com"],
        "numResults": 100,
        "type": "auto",
        "moderation": True,
    }
    assert page.has_more is False
    assert page.next_offset is None
    assert page.hits[0].account_hint == "Acme Jobs"
    assert page.hits[0].backend_date_hint is not None
    assert page.hits[0].snippet is None
    serialized = page.model_dump_json()
    assert "provider-result-id" not in serialized
    assert "must not escape" not in serialized
    assert "request-id-must-not-escape" not in serialized


def test_exa_recall_hints_do_not_become_hard_filters_for_known_article(
    tmp_path,
) -> None:
    known_url = "https://mp.weixin.qq.com/s/Fn8umHSk_LdZ6lz4YPa5Rg"
    observed_bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed_bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "顶尖人才寻人启事｜青云计划2027校招全面启动",
                        "url": known_url,
                        "author": "腾讯",
                        "id": "mock-provider-result",
                    }
                ]
            },
        )

    class VerificationPage:
        def get(self, url: str, expected_accounts: object) -> object:
            assert url == known_url
            raise VerificationRequiredError()

    request = DiscoveryRequest(
        query="2027届 秋招",
        companies=["腾讯"],
        expected_accounts=[
            {"biz_id": "MzA3NDEyMDgzMw==", "display_names": ["腾讯"]}
        ],
        published_after="2026-06-01",
        published_before="2026-09-02",
        hydrate=True,
        allow_browser=False,
    )
    provider = ExaDiscoveryProvider(
        httpx.Client(transport=httpx.MockTransport(handler)),
        "key",
    )
    result = DiscoveryService(
        provider,
        DiscoveryStore(tmp_path / "state.sqlite3"),
        http_evidence=VerificationPage(),  # type: ignore[arg-type]
    ).search(request)

    assert observed_bodies == [
        {
            "query": "2027届 秋招 腾讯",
            "includeDomains": ["mp.weixin.qq.com"],
            "numResults": 100,
            "type": "auto",
            "moderation": True,
        }
    ]
    assert result.summary.received == 1
    assert result.summary.accepted == 1
    assert result.summary.partial is True
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.fetch_url.encoded_string() == known_url
    assert candidate.search_provenance.provider == "exa"
    assert candidate.backend_date_hint is None
    assert candidate.evidence is None
    assert candidate.verification_status == "verification_required"


def test_exa_has_one_bounded_page_and_filters_malformed_items() -> None:
    requests = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            json={
                "results": [
                    None,
                    {"title": 3},
                    {
                        "url": "https://mp.weixin.qq.com/s/T",
                        "title": " ",
                        "author": "a" * 300,
                        "publishedDate": "bad",
                    },
                    {"url": "https://mp.weixin.qq.com/s/" + "x" * 5000},
                ]
            },
        )

    provider = ExaDiscoveryProvider(
        httpx.Client(transport=httpx.MockTransport(handler)),
        "key",
    )
    page = provider.search_page(DiscoveryRequest(query="x"), offset=0, count=500)
    exhausted = provider.search_page(DiscoveryRequest(query="x"), offset=1, count=100)

    assert requests == 1
    assert len(page.hits) == 1
    assert page.hits[0].title is None
    assert len(page.hits[0].account_hint or "") == 200
    assert page.hits[0].backend_date_hint is None
    assert exhausted.hits == []


def test_exa_retries_network_rate_limit_and_server_failures_once() -> None:
    network_calls = 0

    def network_then_ok(request: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        if network_calls == 1:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, json={"results": []})

    network_provider = ExaDiscoveryProvider(
        httpx.Client(transport=httpx.MockTransport(network_then_ok)),
        "key",
    )
    assert network_provider.search_page(
        DiscoveryRequest(query="x"), offset=0, count=100
    ).hits == []
    assert network_calls == 2

    waits: list[float] = []
    statuses = iter((httpx.Response(429, headers={"Retry-After": "120"}), httpx.Response(500)))
    failing_provider = ExaDiscoveryProvider(
        httpx.Client(transport=httpx.MockTransport(lambda _: next(statuses))),
        "key",
        sleep=waits.append,
    )
    with pytest.raises(WxcliError) as raised:
        failing_provider.search_page(DiscoveryRequest(query="x"), offset=0, count=100)
    assert raised.value.code == ErrorCode.NETWORK_ERROR
    assert raised.value.details == {
        "provider": "exa",
        "reason": DiscoveryFailureReason.PROVIDER_ERROR,
    }
    assert waits == [30.0]


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        ("timeout", DiscoveryFailureReason.TIMEOUT),
        ("network", DiscoveryFailureReason.NETWORK_ERROR),
        ("rate", DiscoveryFailureReason.RATE_LIMITED),
    ],
)
def test_exa_exposes_stable_safe_failure_reasons(
    failure: str, reason: DiscoveryFailureReason
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("secret timeout body", request=request)
        if failure == "network":
            raise httpx.ConnectError("secret network body", request=request)
        return httpx.Response(429, text="secret rate body")

    provider = ExaDiscoveryProvider(
        httpx.Client(transport=httpx.MockTransport(handler)),
        "secret-key",
        sleep=lambda _: None,
    )

    with pytest.raises(WxcliError) as raised:
        provider.search_page(DiscoveryRequest(query="x"), offset=0, count=100)

    assert raised.value.code == ErrorCode.NETWORK_ERROR
    assert raised.value.details == {"provider": "exa", "reason": reason}
    serialized = json.dumps(raised.value.details) + str(raised.value)
    assert "secret" not in serialized


def test_exa_empty_search_is_a_successful_empty_page() -> None:
    provider = ExaDiscoveryProvider(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"results": []})
            )
        ),
        "key",
    )

    page = provider.search_page(DiscoveryRequest(query="no matches"), offset=0, count=100)

    assert page == SearchPage(hits=[], has_more=False)


@pytest.mark.parametrize(
    ("status", "code", "reason"),
    [
        (401, ErrorCode.AUTHENTICATION_ERROR, DiscoveryFailureReason.CREDENTIAL_REJECTED),
        (403, ErrorCode.AUTHENTICATION_ERROR, DiscoveryFailureReason.CREDENTIAL_REJECTED),
        (400, ErrorCode.NETWORK_ERROR, DiscoveryFailureReason.PROVIDER_ERROR),
        (402, ErrorCode.NETWORK_ERROR, DiscoveryFailureReason.PROVIDER_ERROR),
    ],
)
def test_exa_maps_provider_failures_without_response_details(
    status: int, code: ErrorCode, reason: DiscoveryFailureReason
) -> None:
    provider = ExaDiscoveryProvider(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(status, text="x-api-key=secret")
            )
        ),
        "secret",
    )

    with pytest.raises(WxcliError) as raised:
        provider.search_page(DiscoveryRequest(query="x"), offset=0, count=100)

    assert raised.value.code == code
    assert raised.value.details == {"provider": "exa", "reason": reason}
    assert "secret" not in str(raised.value)


def test_exa_rejects_empty_key_and_invalid_responses() -> None:
    with pytest.raises(WxcliError) as empty:
        ExaDiscoveryProvider(httpx.Client(), " ")
    assert empty.value.code == ErrorCode.AUTHENTICATION_ERROR
    assert empty.value.details["reason"] == DiscoveryFailureReason.NOT_CONFIGURED

    invalid_json = ExaDiscoveryProvider(
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, text="no"))
        ),
        "key",
    )
    with pytest.raises(WxcliError):
        invalid_json.search_page(DiscoveryRequest(query="x"), offset=0, count=100)

    invalid_results = ExaDiscoveryProvider(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"results": {}})
            )
        ),
        "key",
    )
    with pytest.raises(WxcliError):
        invalid_results.search_page(DiscoveryRequest(query="x"), offset=0, count=100)


def test_brave_sends_site_query_date_filter_and_sanitizes_results() -> None:
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "Acme hiring",
                            "url": "https://mp.weixin.qq.com/s/TOKEN",
                            "description": "Campus jobs",
                            "page_age": "2026-08-01T00:00:00Z",
                            "profile": {"long_name": "Acme Jobs"},
                            "raw_secret": "must not escape",
                        }
                    ]
                }
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = BraveDiscoveryProvider(client, "example-secret")
    page = provider.search_page(
        DiscoveryRequest(
            query="2027 校园招聘",
            companies=["Acme"],
            published_after="2026-01-01",
            published_before="2026-12-31",
        ),
        offset=0,
        count=20,
    )

    assert str(observed[0].url).startswith(BRAVE_SEARCH_URL)
    assert "site%3Amp.weixin.qq.com%2Fs" in str(observed[0].url)
    assert observed[0].url.params["freshness"] == "2026-01-01to2026-12-31"
    assert observed[0].headers["X-Subscription-Token"] == "example-secret"
    assert page.hits[0].account_hint == "Acme Jobs"
    assert page.hits[0].backend_date_hint is not None
    assert "raw_secret" not in page.model_dump_json()


def test_brave_retries_network_once_and_rate_limit_with_bounded_wait() -> None:
    calls = 0
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, json={"web": {"results": []}})

    provider = BraveDiscoveryProvider(
        httpx.Client(transport=httpx.MockTransport(handler)), "key", sleep=waits.append
    )
    assert provider.search_page(DiscoveryRequest(query="x"), offset=0, count=20).hits == []
    assert calls == 2

    rate_calls = 0

    def rate_handler(request: httpx.Request) -> httpx.Response:
        nonlocal rate_calls
        rate_calls += 1
        if rate_calls == 1:
            return httpx.Response(429, headers={"Retry-After": "120"})
        return httpx.Response(200, json={"web": {"results": []}})

    rate_provider = BraveDiscoveryProvider(
        httpx.Client(transport=httpx.MockTransport(rate_handler)), "key", sleep=waits.append
    )
    rate_provider.search_page(DiscoveryRequest(query="x"), offset=0, count=20)
    assert waits == [30.0]


@pytest.mark.parametrize(("status", "code"), [(401, ErrorCode.AUTHENTICATION_ERROR), (500, ErrorCode.NETWORK_ERROR)])
def test_brave_maps_provider_failures(status: int, code: ErrorCode) -> None:
    provider = BraveDiscoveryProvider(
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status))), "key"
    )
    with pytest.raises(WxcliError) as raised:
        provider.search_page(DiscoveryRequest(query="x"), offset=0, count=20)
    assert raised.value.code == code


def test_brave_rejects_invalid_response_and_stops_after_page_nine() -> None:
    invalid = BraveDiscoveryProvider(
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text="no"))),
        "key",
    )
    with pytest.raises(WxcliError):
        invalid.search_page(DiscoveryRequest(query="x"), offset=0, count=20)
    assert invalid.search_page(DiscoveryRequest(query="x"), offset=10, count=20).hits == []


def test_brave_filters_malformed_items_and_maps_other_edge_responses() -> None:
    result_payload = {
        "web": {
            "results": [
                None,
                {"title": 3},
                {"url": "https://mp.weixin.qq.com/s/T", "title": " ", "age": "bad"},
            ]
        }
    }
    provider = BraveDiscoveryProvider(
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=result_payload))
        ),
        "key",
    )
    page = provider.search_page(DiscoveryRequest(query="x"), offset=0, count=2)
    assert len(page.hits) == 1
    assert page.hits[0].title is None
    assert page.hits[0].backend_date_hint is None
    assert page.has_more is True

    malformed = BraveDiscoveryProvider(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"web": {"results": {}}})
            )
        ),
        "key",
    )
    with pytest.raises(WxcliError):
        malformed.search_page(DiscoveryRequest(query="x"), offset=0, count=20)

    forbidden = BraveDiscoveryProvider(
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(400))), "key"
    )
    with pytest.raises(WxcliError):
        forbidden.search_page(DiscoveryRequest(query="x"), offset=0, count=20)

    with pytest.raises(WxcliError):
        BraveDiscoveryProvider(httpx.Client(), " ")


def test_brave_bounds_provider_text_and_skips_oversized_urls() -> None:
    payload = {
        "web": {
            "results": [
                {
                    "url": "https://mp.weixin.qq.com/s/T",
                    "title": "t" * 700,
                    "description": "d" * 6000,
                    "profile": {"long_name": "a" * 300},
                },
                {"url": "https://mp.weixin.qq.com/s/" + "x" * 5000},
            ]
        }
    }
    provider = BraveDiscoveryProvider(
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
        ),
        "key",
    )

    page = provider.search_page(DiscoveryRequest(query="x"), offset=0, count=20)

    assert len(page.hits) == 1
    assert len(page.hits[0].title or "") == 500
    assert len(page.hits[0].snippet or "") == 5000
    assert len(page.hits[0].account_hint or "") == 200


def test_brave_second_network_and_rate_limit_failures_do_not_loop() -> None:
    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with pytest.raises(WxcliError) as network:
        BraveDiscoveryProvider(
            httpx.Client(transport=httpx.MockTransport(offline)), "key"
        ).search_page(DiscoveryRequest(query="x"), offset=0, count=20)
    assert network.value.code == ErrorCode.NETWORK_ERROR

    waits: list[float] = []
    rate = BraveDiscoveryProvider(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(429, headers={"Retry-After": "not-a-number"})
            )
        ),
        "key",
        sleep=waits.append,
    )
    with pytest.raises(WxcliError):
        rate.search_page(DiscoveryRequest(query="x"), offset=0, count=20)
    assert waits == [1.0]


def test_redaction_covers_discovery_credentials() -> None:
    assert redact({"api_key": "a", "X-Subscription-Token": "b"}) == {
        "api_key": "[REDACTED]",
        "X-Subscription-Token": "[REDACTED]",
    }
    assert "secret" not in redact_text("x-api-key=secret")


def test_store_cache_history_expiry_concurrency_and_clear(tmp_path) -> None:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    store = DiscoveryStore(tmp_path / "state.sqlite3")
    page = SearchPage(
        hits=[SearchHit(url="https://mp.weixin.qq.com/s/T", rank=1, result_id="r")],
        has_more=False,
    )
    store.put_page("brave", "fingerprint", 0, page, now)
    assert store.get_page("brave", "fingerprint", 0, now) == page
    assert store.get_page("brave", "fingerprint", 0, now + timedelta(minutes=16)) is None

    first = store.observe_candidate("fingerprint", "token:T", "https://mp.weixin.qq.com/s/T", now)
    second = store.observe_candidate(
        "fingerprint", "token:T", "https://mp.weixin.qq.com/s/T", now + timedelta(hours=1)
    )
    assert first[2] is True
    assert second[0] == first[0]
    assert second[2] is False

    def observe(index: int) -> bool:
        return store.observe_candidate(
            "fingerprint",
            f"token:{index}",
            f"https://mp.weixin.qq.com/s/{index}",
            now,
        )[2]

    with ThreadPoolExecutor(max_workers=4) as executor:
        assert all(executor.map(observe, range(8)))

    assert store.prune(now + timedelta(days=181)) >= 9
    store.put_page("brave", "fingerprint", 0, page, now)
    store.observe_candidate("fingerprint", "token:new", "https://mp.weixin.qq.com/s/new", now)
    store.put_checkpoint("brave", "fingerprint", now)
    assert store.clear() == 3


def test_store_ignores_corrupt_cached_page_and_reports_unavailable_database(tmp_path) -> None:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    store = DiscoveryStore(tmp_path / "state.sqlite3")
    page = SearchPage(hits=[], has_more=False)
    store.put_page("brave", "fp", 0, page, now)
    import sqlite3

    connection = sqlite3.connect(store.path)
    connection.execute("UPDATE search_cache SET response_json = 'bad'")
    connection.commit()
    connection.close()
    assert store.get_page("brave", "fp", 0, now) is None

    blocked = tmp_path / "a-file"
    blocked.write_text("x", encoding="utf-8")
    with pytest.raises(WxcliError) as raised:
        DiscoveryStore(blocked / "state.sqlite3").clear()
    assert raised.value.code == ErrorCode.LOCAL_CONFIGURATION_ERROR
