"""Tests for keyring-backed secrets, the stable token cache, and controlled refresh retry."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from wxcli.auth import (
    AccessToken,
    AccessTokenInvalid,
    AccessTokenStore,
    AppIdStore,
    SecretStore,
    TokenManager,
    raise_for_official_error,
    raise_if_token_invalid,
)
from wxcli.errors import ErrorCode, ValidationError, WxcliError

SECRET = "test-secret"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


class FakeBackend:
    """An in-memory PasswordBackend standing in for the Windows credential store."""

    def __init__(self, fail: bool = False) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.fail = fail

    def get_password(self, service_name: str, username: str) -> str | None:
        if self.fail:
            raise RuntimeError("backend down")
        return self.store.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        if self.fail:
            raise RuntimeError("backend down")
        self.store[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.store.pop((service_name, username), None)


def make_manager(
    backend: FakeBackend,
    state_path: Path,
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
) -> tuple[TokenManager, list[dict[str, object]]]:
    requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if handler is not None:
            return handler(request)
        return httpx.Response(200, json={"access_token": f"token-{len(requests)}", "expires_in": 7200})

    manager = TokenManager(
        httpx.Client(transport=httpx.MockTransport(respond)),
        "test-appid",
        SecretStore(backend),
        AccessTokenStore(backend, state_path),
        now=lambda: NOW,
    )
    return manager, requests


def test_secret_store_roundtrip_and_clear() -> None:
    backend = FakeBackend()
    store = SecretStore(backend)

    store.set_app_secret(SECRET)

    assert store.get_app_secret() == SECRET
    store.clear()
    assert store.get_app_secret() is None


def test_secret_store_rejects_empty_secret() -> None:
    with pytest.raises(ValidationError):
        SecretStore(FakeBackend()).set_app_secret("   ")


def test_secret_store_wraps_backend_failure() -> None:
    with pytest.raises(WxcliError) as error:
        SecretStore(FakeBackend(fail=True)).get_app_secret()
    assert error.value.code is ErrorCode.LOCAL_CONFIGURATION_ERROR


def test_appid_uses_plain_config_without_secret(tmp_path: Path) -> None:
    store = AppIdStore(tmp_path / "config.json")
    store.put("test-appid")
    assert store.get() == "test-appid"
    assert "secret" not in (tmp_path / "config.json").read_text(encoding="utf-8").lower()


def test_token_store_ignores_missing_malformed_and_stale_entries(tmp_path: Path) -> None:
    backend = FakeBackend()
    state_path = tmp_path / "token-state.json"
    store = AccessTokenStore(backend, state_path)
    assert store.get(NOW) is None

    backend.store[("wxcli", "access_token")] = "cached-token"
    state_path.write_text("not-json", encoding="utf-8")
    assert store.get(NOW) is None

    store.put(AccessToken("soon-expired", NOW + timedelta(minutes=4)))
    assert store.get(NOW) is None

    store.put(AccessToken("fresh", NOW + timedelta(hours=2)))
    assert store.get(NOW) == AccessToken("fresh", NOW + timedelta(hours=2))
    assert backend.store[("wxcli", "access_token")] == "fresh"
    assert "fresh" not in state_path.read_text(encoding="utf-8")


def test_get_token_reuses_the_stable_cached_token(tmp_path: Path) -> None:
    backend = FakeBackend()
    SecretStore(backend).set_app_secret(SECRET)
    manager, requests = make_manager(backend, tmp_path / "token-state.json")

    assert manager.get_token() == "token-1"
    assert manager.get_token() == "token-1"
    assert len(requests) == 1
    assert requests[0]["force_refresh"] is False


def test_refresh_without_secret_is_an_authentication_error(tmp_path: Path) -> None:
    manager, requests = make_manager(FakeBackend(), tmp_path / "token-state.json")

    with pytest.raises(WxcliError) as error:
        manager.get_token()
    assert error.value.code is ErrorCode.AUTHENTICATION_ERROR
    assert requests == []


def test_refresh_failure_never_exposes_the_secret(tmp_path: Path) -> None:
    backend = FakeBackend()
    SecretStore(backend).set_app_secret(SECRET)
    manager, _ = make_manager(
        backend,
        tmp_path / "token-state.json",
        lambda request: httpx.Response(200, json={"errcode": 40125, "errmsg": "invalid appsecret"}),
    )

    with pytest.raises(WxcliError) as error:
        manager.get_token()
    assert error.value.code is ErrorCode.AUTHENTICATION_ERROR
    assert SECRET not in str(error.value)
    assert SECRET not in json.dumps(error.value.details)


def test_refresh_network_error_is_a_network_error(tmp_path: Path) -> None:
    backend = FakeBackend()
    SecretStore(backend).set_app_secret(SECRET)
    manager, _ = make_manager(
        backend,
        tmp_path / "token-state.json",
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("down")),
    )

    with pytest.raises(WxcliError) as error:
        manager.get_token()
    assert error.value.code is ErrorCode.NETWORK_ERROR


@pytest.mark.parametrize("errcode", [40001, 40014, 42001])
def test_raise_if_token_invalid_flags_only_token_errcodes(errcode: int) -> None:
    raise_if_token_invalid({"errcode": 0})
    raise_if_token_invalid({"errcode": 48001})
    with pytest.raises(AccessTokenInvalid):
        raise_if_token_invalid({"errcode": errcode})


@pytest.mark.parametrize(
    ("errcode", "expected"),
    [
        (40164, ErrorCode.AUTHENTICATION_ERROR),
        (61004, ErrorCode.AUTHENTICATION_ERROR),
        (48001, ErrorCode.AUTHENTICATION_ERROR),
        (45009, ErrorCode.NETWORK_ERROR),
        (45011, ErrorCode.NETWORK_ERROR),
    ],
)
def test_official_error_mapping(errcode: int, expected: ErrorCode) -> None:
    with pytest.raises(WxcliError) as raised:
        raise_for_official_error({"errcode": errcode, "errmsg": "not copied"})
    assert raised.value.code is expected
    assert raised.value.details == {"errcode": errcode}


def test_with_token_retry_force_refreshes_once_and_retries(tmp_path: Path) -> None:
    backend = FakeBackend()
    SecretStore(backend).set_app_secret(SECRET)
    manager, requests = make_manager(backend, tmp_path / "token-state.json")
    attempts: list[str] = []

    def call(token: str) -> str:
        attempts.append(token)
        if len(attempts) == 1:
            raise AccessTokenInvalid(40001)
        return "ok"

    assert manager.with_token_retry(call) == "ok"
    assert attempts == ["token-1", "token-2"]
    assert [request["force_refresh"] for request in requests] == [False, True]


def test_with_token_retry_gives_up_after_one_retry(tmp_path: Path) -> None:
    backend = FakeBackend()
    SecretStore(backend).set_app_secret(SECRET)
    manager, requests = make_manager(backend, tmp_path / "token-state.json")

    def call(token: str) -> str:
        raise AccessTokenInvalid(42001)

    with pytest.raises(AccessTokenInvalid):
        manager.with_token_retry(call)
    assert len(requests) == 2
