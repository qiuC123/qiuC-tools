"""Mocked tests for explicitly authorized read-only Official Account checks."""

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from wxcli.errors import ErrorCode, WxcliError
from wxcli.official_check import OfficialReadOnlyChecker


class FakeTokens:
    def __init__(self) -> None:
        self.calls = 0

    def with_token_retry(self, call: Callable[[str], Any]) -> Any:
        self.calls += 1
        return call("fake-token")

    def get_token(self) -> str:
        return "fake-token"


def test_read_only_checker_calls_only_two_batch_list_endpoints() -> None:
    paths: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.method == "POST"
        assert request.url.params.get("access_token") == "fake-token"
        return httpx.Response(200, json={"total_count": 2, "item_count": 1, "item": []})

    tokens = FakeTokens()
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = OfficialReadOnlyChecker(client, tokens).run()

    assert paths == ["/cgi-bin/draft/batchget", "/cgi-bin/freepublish/batchget"]
    assert tokens.calls == 2
    assert result == {
        "stable_token": "pass",
        "ip_allowlist": "pass",
        "draft_batchget": {"total_count": 2, "item_count": 1},
        "freepublish_batchget": {"total_count": 2, "item_count": 1},
    }


def test_read_only_checker_maps_ip_allowlist_failure() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"errcode": 40164, "errmsg": "denied"})
    )
    with httpx.Client(transport=transport) as client, pytest.raises(WxcliError) as raised:
        OfficialReadOnlyChecker(client, FakeTokens()).run()
    assert raised.value.code is ErrorCode.AUTHENTICATION_ERROR
    assert raised.value.details == {
        "errcode": 40164,
        "check": "draft_batchget",
        "checks": {
            "stable_token": "pass",
            "ip_allowlist": "pass",
            "draft_batchget": "fail",
        },
    }


def test_read_only_checker_identifies_the_denied_endpoint() -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"total_count": 0, "item_count": 0})
        return httpx.Response(200, json={"errcode": 48001, "errmsg": "api unauthorized"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client, pytest.raises(
        WxcliError
    ) as raised:
        OfficialReadOnlyChecker(client, FakeTokens()).run()
    assert raised.value.details == {
        "errcode": 48001,
        "check": "freepublish_batchget",
        "checks": {
            "stable_token": "pass",
            "ip_allowlist": "pass",
            "draft_batchget": "pass",
            "freepublish_batchget": "fail",
        },
    }
