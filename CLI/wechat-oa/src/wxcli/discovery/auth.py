"""Discovery-only credential storage."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from wxcli.auth import PasswordBackend
from wxcli.discovery.provider import DiscoveryProviderName
from wxcli.errors import ErrorCode, ValidationError, WxcliError

BRAVE_SERVICE_NAME = "wxcli.discovery.brave"
EXA_SERVICE_NAME = "wxcli.discovery.exa"
_API_KEY = "api_key"
T = TypeVar("T")

_SERVICE_NAMES: dict[DiscoveryProviderName, str] = {
    "brave": BRAVE_SERVICE_NAME,
    "exa": EXA_SERVICE_NAME,
}


class DiscoverySecretStore:
    """Keep provider keys in keyring, separate from Official Account secrets."""

    def __init__(self, backend: PasswordBackend) -> None:
        self._backend = backend

    def get_brave_api_key(self) -> str | None:
        return self.get_api_key("brave")

    def set_brave_api_key(self, value: str) -> None:
        self.set_api_key("brave", value)

    def get_api_key(self, provider: DiscoveryProviderName) -> str | None:
        service_name = _SERVICE_NAMES[provider]
        return self._call(lambda: self._backend.get_password(service_name, _API_KEY))

    def set_api_key(self, provider: DiscoveryProviderName, value: str) -> None:
        if not value.strip():
            raise ValidationError(f"The {provider.title()} API key must not be empty.")
        service_name = _SERVICE_NAMES[provider]
        self._call(
            lambda: self._backend.set_password(service_name, _API_KEY, value)
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
