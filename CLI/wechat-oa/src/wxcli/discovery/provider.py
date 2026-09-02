"""Replaceable Discovery Provider interface."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Protocol

from wxcli.discovery.models import DiscoveryRequest, SearchPage
from wxcli.errors import ValidationError

DiscoveryProviderName = Literal["brave", "exa"]
SUPPORTED_DISCOVERY_PROVIDERS: tuple[DiscoveryProviderName, ...] = ("brave", "exa")


class DiscoveryFailureReason(StrEnum):
    """Stable provider-failure reasons carried in JSON error details."""

    NOT_CONFIGURED = "not_configured"
    CREDENTIAL_REJECTED = "credential_rejected"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    PROVIDER_ERROR = "provider_error"
    INVALID_RESPONSE = "invalid_response"


def normalize_discovery_provider(value: str) -> DiscoveryProviderName:
    normalized = value.strip().casefold()
    if normalized not in SUPPORTED_DISCOVERY_PROVIDERS:
        supported = ", ".join(SUPPORTED_DISCOVERY_PROVIDERS)
        raise ValidationError(f"Unsupported discovery provider. Choose one of: {supported}.")
    return normalized


class DiscoveryProvider(Protocol):
    name: str
    page_size: int

    def search_page(
        self, request: DiscoveryRequest, *, offset: int, count: int
    ) -> SearchPage: ...
