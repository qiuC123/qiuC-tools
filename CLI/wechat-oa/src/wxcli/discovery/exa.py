"""Exa Search implementation of the Discovery Provider contract."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

import httpx

from wxcli.discovery.models import DiscoveryRequest, SearchHit, SearchPage
from wxcli.discovery.provider import DiscoveryFailureReason
from wxcli.errors import ErrorCode, WxcliError

EXA_SEARCH_URL = "https://api.exa.ai/search"
_EXA_RESULT_LIMIT = 100


class ExaDiscoveryProvider:
    name = "exa"
    page_size = _EXA_RESULT_LIMIT

    def __init__(
        self,
        client: httpx.Client,
        api_key: str,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key.strip():
            raise WxcliError(
                ErrorCode.AUTHENTICATION_ERROR,
                "The Exa API key is not configured.",
                {"provider": self.name, "reason": DiscoveryFailureReason.NOT_CONFIGURED},
            )
        self._client = client
        self._api_key = api_key
        self._sleep = sleep

    def search_page(
        self, request: DiscoveryRequest, *, offset: int, count: int
    ) -> SearchPage:
        if offset != 0:
            return SearchPage(hits=[], has_more=False)
        payload: dict[str, Any] = {
            "query": _build_query(request),
            "includeDomains": ["mp.weixin.qq.com"],
            "numResults": min(max(count, 1), _EXA_RESULT_LIMIT),
            "type": "auto",
            "moderation": True,
        }
        response = self._request(payload)
        try:
            response_payload = response.json()
        except ValueError as error:
            raise WxcliError(
                ErrorCode.NETWORK_ERROR,
                "The discovery provider returned invalid JSON.",
                {"provider": self.name, "reason": DiscoveryFailureReason.INVALID_RESPONSE},
            ) from error
        results = (
            response_payload.get("results", [])
            if isinstance(response_payload, dict)
            else []
        )
        if not isinstance(results, list):
            raise WxcliError(
                ErrorCode.NETWORK_ERROR,
                "The discovery provider returned an invalid response.",
                {"provider": self.name, "reason": DiscoveryFailureReason.INVALID_RESPONSE},
            )
        hits: list[SearchHit] = []
        for index, value in enumerate(results):
            if not isinstance(value, Mapping) or not isinstance(value.get("url"), str):
                continue
            url = str(value["url"])
            if len(url) > 4096:
                continue
            raw_result_id = value.get("id")
            identity = raw_result_id if isinstance(raw_result_id, str) else url
            hits.append(
                SearchHit(
                    title=_optional_string(value.get("title"), 500),
                    url=url,
                    snippet=None,
                    account_hint=_optional_string(value.get("author"), 200),
                    backend_date_hint=_date_hint(value),
                    rank=index + 1,
                    result_id=hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
                )
            )
        return SearchPage(hits=hits, has_more=False)

    def _request(self, payload: dict[str, Any]) -> httpx.Response:
        for attempt in range(2):
            try:
                response = self._client.post(
                    EXA_SEARCH_URL,
                    json=payload,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "x-api-key": self._api_key,
                    },
                )
            except httpx.TimeoutException as error:
                if attempt == 0:
                    continue
                raise WxcliError(
                    ErrorCode.NETWORK_ERROR,
                    "The discovery provider request timed out.",
                    {"provider": self.name, "reason": DiscoveryFailureReason.TIMEOUT},
                ) from error
            except httpx.HTTPError as error:
                if attempt == 0:
                    continue
                raise WxcliError(
                    ErrorCode.NETWORK_ERROR,
                    "The discovery provider could not be reached.",
                    {"provider": self.name, "reason": DiscoveryFailureReason.NETWORK_ERROR},
                ) from error
            if response.status_code in {401, 403}:
                raise WxcliError(
                    ErrorCode.AUTHENTICATION_ERROR,
                    "The discovery provider rejected its configured credential.",
                    {
                        "provider": self.name,
                        "reason": DiscoveryFailureReason.CREDENTIAL_REJECTED,
                    },
                )
            if response.status_code == 429:
                if attempt == 0:
                    self._sleep(_retry_after(response))
                    continue
                raise WxcliError(
                    ErrorCode.NETWORK_ERROR,
                    "The discovery provider rate limit was reached.",
                    {"provider": self.name, "reason": DiscoveryFailureReason.RATE_LIMITED},
                )
            if response.status_code >= 500 and attempt == 0:
                continue
            if response.status_code >= 400:
                raise WxcliError(
                    ErrorCode.NETWORK_ERROR,
                    "The discovery provider returned an error.",
                    {"provider": self.name, "reason": DiscoveryFailureReason.PROVIDER_ERROR},
                )
            return response
        raise WxcliError(
            ErrorCode.NETWORK_ERROR,
            "The discovery provider could not be reached.",
            {"provider": self.name, "reason": DiscoveryFailureReason.NETWORK_ERROR},
        )


def _build_query(request: DiscoveryRequest) -> str:
    terms = [*request.companies]
    for account in request.expected_accounts:
        terms.extend(account.display_names)
    terms.append("官方公众号")
    terms.append(request.query)
    terms.extend(_query_expansions(request.query))
    unique_terms: list[str] = []
    seen: set[str] = set()
    for term in terms:
        identity = " ".join(term.split()).casefold()
        if identity in seen:
            continue
        seen.add(identity)
        unique_terms.append(term)
    return " ".join(unique_terms)


def _query_expansions(value: str) -> list[str]:
    """Add small lexical variants without turning provider hints into evidence."""

    normalized = " ".join(value.split())
    expansions = re.findall(r"(?<!\d)(\d{4})届", normalized)
    if "秋招" in normalized:
        expansions.extend(("校招", "校园招聘", "秋季招聘"))
    if "春招" in normalized:
        expansions.extend(("校招", "校园招聘", "春季招聘"))
    if "校园招聘" in normalized and "校招" not in normalized:
        expansions.append("校招")
    return expansions


def _optional_string(value: object, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    clean = value.strip()
    return clean[:maximum] if clean else None


def _date_hint(value: Mapping[str, Any]) -> date | None:
    raw = value.get("publishedDate")
    if not isinstance(raw, str) or len(raw) < 10:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None
def _retry_after(response: httpx.Response) -> float:
    try:
        return min(max(float(response.headers.get("Retry-After", "1")), 0.0), 30.0)
    except ValueError:
        return 1.0
