"""Discovery-only credential storage."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from wxcli.auth import PasswordBackend
from wxcli.errors import ErrorCode, ValidationError, WxcliError

BRAVE_SERVICE_NAME = "wxcli.discovery.brave"
_BRAVE_API_KEY = "api_key"
T = TypeVar("T")


class DiscoverySecretStore:
    """Keep the Brave key in keyring, separate from Official Account secrets."""

    def __init__(self, backend: PasswordBackend) -> None:
        self._backend = backend

    def get_brave_api_key(self) -> str | None:
        return self._call(
            lambda: self._backend.get_password(BRAVE_SERVICE_NAME, _BRAVE_API_KEY)
        )

    def set_brave_api_key(self, value: str) -> None:
        if not value.strip():
            raise ValidationError("The Brave API key must not be empty.")
        self._call(
            lambda: self._backend.set_password(BRAVE_SERVICE_NAME, _BRAVE_API_KEY, value)
        )

    @staticmethod
    def _call(operation: Callable[[], T]) -> T:
        try:
            return operation()
        except Exception as error:
            raise WxcliError(
                ErrorCode.LOCAL_CONFIGURATION_ERROR,
                "The Windows credential store is unavailable.",
            ) from error
