"""Brave Web Search implementation of the Discovery Provider contract."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

import httpx

from wxcli.discovery.models import DiscoveryRequest, SearchHit, SearchPage
from wxcli.errors import ErrorCode, WxcliError

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"


class BraveDiscoveryProvider:
    name = "brave"

    def __init__(
        self,
        client: httpx.Client,
        api_key: str,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key.strip():
            raise WxcliError(ErrorCode.AUTHENTICATION_ERROR, "The Brave API key is not configured.")
        self._client = client
        self._api_key = api_key
        self._sleep = sleep

    def search_page(
        self, request: DiscoveryRequest, *, offset: int, count: int
    ) -> SearchPage:
        if offset < 0 or offset > 9:
            return SearchPage(hits=[], has_more=False)
        params: dict[str, str | int] = {
            "q": _build_query(request),
            "count": min(max(count, 1), 20),
            "offset": offset,
            "safesearch": "moderate",
        }
        if request.published_after and request.published_before:
            params["freshness"] = (
                f"{request.published_after.isoformat()}to{request.published_before.isoformat()}"
            )
        response = self._request(params)
        try:
            payload = response.json()
        except ValueError as error:
            raise WxcliError(
                ErrorCode.NETWORK_ERROR, "The discovery provider returned invalid JSON."
            ) from error
        results = payload.get("web", {}).get("results", []) if isinstance(payload, dict) else []
        if not isinstance(results, list):
            raise WxcliError(
                ErrorCode.NETWORK_ERROR, "The discovery provider returned an invalid response."
            )
        hits: list[SearchHit] = []
        base_rank = offset * int(params["count"])
        for index, value in enumerate(results):
            if not isinstance(value, Mapping) or not isinstance(value.get("url"), str):
                continue
            url = str(value["url"])
            if len(url) > 4096:
                continue
            profile = value.get("profile")
            account_hint = (
                _optional_string(profile.get("long_name"), 200)
                if isinstance(profile, Mapping) and isinstance(profile.get("long_name"), str)
                else None
            )
            hits.append(
                SearchHit(
                    title=_optional_string(value.get("title"), 500),
                    url=url,
                    snippet=_optional_string(value.get("description"), 5000),
                    account_hint=account_hint,
                    backend_date_hint=_date_hint(value),
                    rank=base_rank + index + 1,
                    result_id=hashlib.sha256(url.encode("utf-8")).hexdigest()[:24],
                )
            )
        has_more = len(results) >= int(params["count"]) and offset < 9
        return SearchPage(
            hits=hits,
            has_more=has_more,
            next_offset=offset + 1 if has_more else None,
        )

    def _request(self, params: dict[str, str | int]) -> httpx.Response:
        for attempt in range(2):
            try:
                response = self._client.get(
                    BRAVE_SEARCH_URL,
                    params=params,
                    headers={
                        "Accept": "application/json",
                        "X-Subscription-Token": self._api_key,
                    },
                )
            except httpx.HTTPError as error:
                if attempt == 0:
                    continue
                raise WxcliError(
                    ErrorCode.NETWORK_ERROR, "The discovery provider could not be reached."
                ) from error
            if response.status_code in {401, 403}:
                raise WxcliError(
                    ErrorCode.AUTHENTICATION_ERROR,
                    "The discovery provider rejected its configured credential.",
                )
            if response.status_code == 429:
                if attempt == 0:
                    self._sleep(_retry_after(response))
                    continue
                raise WxcliError(
                    ErrorCode.NETWORK_ERROR, "The discovery provider rate limit was reached."
                )
            if response.status_code >= 500 and attempt == 0:
                continue
            if response.status_code >= 400:
                raise WxcliError(
                    ErrorCode.NETWORK_ERROR, "The discovery provider returned an error."
                )
            return response
        raise WxcliError(ErrorCode.NETWORK_ERROR, "The discovery provider could not be reached.")


def _build_query(request: DiscoveryRequest) -> str:
    terms = ["site:mp.weixin.qq.com/s", request.query]
    terms.extend(f'"{value}"' for value in request.companies)
    for account in request.expected_accounts:
        terms.extend(f'"{value}"' for value in account.display_names)
    return " ".join(terms)


def _optional_string(value: object, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    clean = value.strip()
    return clean[:maximum] if clean else None


def _date_hint(value: Mapping[str, Any]) -> date | None:
    for key in ("page_age", "age"):
        raw = value.get(key)
        if not isinstance(raw, str) or len(raw) < 10:
            continue
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            continue
    return None


def _retry_after(response: httpx.Response) -> float:
    try:
        return min(max(float(response.headers.get("Retry-After", "1")), 0.0), 30.0)
    except ValueError:
        return 1.0
