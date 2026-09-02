"""Replaceable Discovery Provider interface."""

from __future__ import annotations

from typing import Protocol

from wxcli.discovery.models import DiscoveryRequest, SearchPage


class DiscoveryProvider(Protocol):
    name: str
    page_size: int

    def search_page(
        self, request: DiscoveryRequest, *, offset: int, count: int
    ) -> SearchPage: ...
