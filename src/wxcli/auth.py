"""Keyring-backed AppSecret storage, a stable access-token cache, and controlled token refresh."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, TypeVar

import httpx

from wxcli.errors import ErrorCode, ValidationError, WxcliError

SERVICE_NAME = "wxcli"
_APP_SECRET_KEY = "app_secret"
_ACCESS_TOKEN_KEY = "access_token"
_STABLE_TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/stable_token"
_STALE_MARGIN = timedelta(minutes=5)
_TOKEN_INVALID_ERRCODES = {40001, 40014, 42001}

T = TypeVar("T")


class PasswordBackend(Protocol):
    """The small subset of the keyring API wxcli relies on."""

    def get_password(self, service_name: str, username: str) -> str | None: ...

    def set_password(self, service_name: str, username: str, password: str) -> None: ...

    def delete_password(self, service_name: str, username: str) -> None: ...


def default_backend() -> PasswordBackend:
    """Return the operating-system credential store through keyring."""
    import keyring

    return keyring


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SecretStore:
    """Keep the AppSecret in the Windows credential store, never on disk or in arguments."""

    def __init__(self, backend: PasswordBackend) -> None:
        self._backend = backend

    def get_app_secret(self) -> str | None:
        return self._call(lambda: self._backend.get_password(SERVICE_NAME, _APP_SECRET_KEY))

    def set_app_secret(self, secret: str) -> None:
        if not secret.strip():
            raise ValidationError("The AppSecret must not be empty.")
        self._call(lambda: self._backend.set_password(SERVICE_NAME, _APP_SECRET_KEY, secret))

    def clear(self) -> None:
        try:
            self._backend.delete_password(SERVICE_NAME, _APP_SECRET_KEY)
        except Exception:
            # Deleting an absent secret or hitting an unavailable store is a no-op.
            return

    @staticmethod
    def _call(operation: Callable[[], T]) -> T:
        try:
            return operation()
        except Exception as error:
            raise WxcliError(
                ErrorCode.LOCAL_CONFIGURATION_ERROR, "The Windows credential store is unavailable."
            ) from error


class AppIdStore:
    """Store the non-secret AppID in an ordinary UTF-8 JSON configuration file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def get(self) -> str | None:
        try:
            value = json.loads(self._path.read_text(encoding="utf-8")).get("appid")
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, str) and value else None

    def put(self, appid: str) -> None:
        if not appid.strip():
            raise ValidationError("The AppID must not be empty.")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"appid": appid}, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        except OSError as error:
            raise WxcliError(
                ErrorCode.LOCAL_CONFIGURATION_ERROR, "The AppID configuration could not be saved."
            ) from error


@dataclass(frozen=True, slots=True)
class AccessToken:
    """An Official Account access token with its absolute expiry."""

    value: str
    expires_at: datetime

    def is_stale(self, now: datetime) -> bool:
        """Treat the token as unusable shortly before expiry so it never dies mid-call."""
        return now >= self.expires_at - _STALE_MARGIN


class AccessTokenStore:
    """Keep the token in keyring and only its non-secret expiry in a state file."""

    def __init__(self, backend: PasswordBackend, state_path: Path) -> None:
        self._backend = backend
        self._state_path = state_path

    def get(self, now: datetime) -> AccessToken | None:
        try:
            value = self._backend.get_password(SERVICE_NAME, _ACCESS_TOKEN_KEY)
        except Exception:
            return None
        if not value:
            return None
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
            token = AccessToken(
                value=value,
                expires_at=datetime.fromisoformat(str(payload["expires_at"])),
            )
        except (FileNotFoundError, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if token.expires_at.tzinfo is None:
            return None
        return None if token.is_stale(now) else token

    def put(self, token: AccessToken) -> None:
        try:
            self._backend.set_password(SERVICE_NAME, _ACCESS_TOKEN_KEY, token.value)
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps({"expires_at": token.expires_at.isoformat()}, separators=(",", ":")),
                encoding="utf-8",
            )
        except Exception as error:
            raise WxcliError(
                ErrorCode.LOCAL_CONFIGURATION_ERROR, "The Windows credential store is unavailable."
            ) from error

    def clear(self) -> None:
        try:
            self._backend.delete_password(SERVICE_NAME, _ACCESS_TOKEN_KEY)
        except Exception:
            pass
        self._state_path.unlink(missing_ok=True)


class AccessTokenInvalid(WxcliError):
    """The Official Account API rejected the current access token."""

    def __init__(self, errcode: int) -> None:
        super().__init__(
            ErrorCode.AUTHENTICATION_ERROR,
            "The Official Account API rejected the access token.",
            {"errcode": errcode},
        )


def raise_if_token_invalid(payload: Mapping[str, Any]) -> None:
    """Raise AccessTokenInvalid when an API payload reports a token-specific errcode."""
    errcode = payload.get("errcode", 0)
    if isinstance(errcode, int) and errcode in _TOKEN_INVALID_ERRCODES:
        raise AccessTokenInvalid(errcode)


def raise_for_official_error(payload: Mapping[str, Any]) -> None:
    """Map documented Official Account API errors without exposing response secrets."""
    errcode = payload.get("errcode", 0)
    if not isinstance(errcode, int) or errcode == 0:
        return
    raise_if_token_invalid(payload)
    if errcode in {40164, 61004}:
        raise WxcliError(
            ErrorCode.AUTHENTICATION_ERROR,
            "The current public IP is not allowed by the Official Account configuration.",
            {"errcode": errcode},
        )
    if errcode == 48001:
        raise WxcliError(
            ErrorCode.AUTHENTICATION_ERROR,
            "The Official Account does not have permission for this API.",
            {"errcode": errcode},
        )
    if errcode in {45009, 45011}:
        raise WxcliError(
            ErrorCode.NETWORK_ERROR,
            "The Official Account API rate limit was reached.",
            {"errcode": errcode},
        )
    raise WxcliError(
        ErrorCode.GENERAL_ERROR,
        "The Official Account API returned an error.",
        {"errcode": errcode},
    )


class TokenManager:
    """Issue stable access tokens and refresh them in a controlled way.

    The cached token is reused until it turns stale, so repeated and concurrent
    runs share one token instead of invalidating each other. A rejected token is
    refreshed exactly once per call through ``with_token_retry``.
    """

    def __init__(
        self,
        client: httpx.Client,
        appid: str,
        secret_store: SecretStore,
        token_store: AccessTokenStore,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._client = client
        self._appid = appid
        self._secret_store = secret_store
        self._token_store = token_store
        self._now = now

    def get_token(self) -> str:
        """Return the cached stable token, refreshing only when it is missing or stale."""
        if token := self._token_store.get(self._now()):
            return token.value
        return self.refresh()

    def refresh(self, *, force: bool = False) -> str:
        """Request a fresh token from the stable-token endpoint and cache it."""
        secret = self._secret_store.get_app_secret()
        if not secret:
            raise WxcliError(ErrorCode.AUTHENTICATION_ERROR, "The AppSecret is not configured.")
        try:
            response = self._client.post(
                _STABLE_TOKEN_URL,
                json={
                    "grant_type": "client_credential",
                    "appid": self._appid,
                    "secret": secret,
                    "force_refresh": force,
                },
            )
        except httpx.HTTPError as error:
            raise WxcliError(ErrorCode.NETWORK_ERROR, "The access token could not be refreshed.") from error
        try:
            payload = response.json()
        except ValueError as error:
            raise WxcliError(ErrorCode.NETWORK_ERROR, "The token endpoint returned an invalid response.") from error
        access_token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if response.status_code != 200 or not isinstance(access_token, str) or not isinstance(expires_in, int | float):
            raise WxcliError(
                ErrorCode.AUTHENTICATION_ERROR,
                "The Official Account API refused the AppID and AppSecret.",
                {"errcode": payload.get("errcode", "unknown")},
            )
        token = AccessToken(access_token, self._now() + timedelta(seconds=int(expires_in)))
        self._token_store.put(token)
        return token.value

    def with_token_retry(self, call: Callable[[str], T]) -> T:
        """Run an API call; if the token is rejected, force-refresh once and retry once."""
        try:
            return call(self.get_token())
        except AccessTokenInvalid:
            return call(self.refresh(force=True))
