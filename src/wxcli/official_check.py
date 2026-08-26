"""Explicitly authorized, read-only Official Account API checks."""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from wxcli.auth import raise_for_official_error
from wxcli.errors import ErrorCode, WxcliError

_DRAFT_BATCHGET_URL = "https://api.weixin.qq.com/cgi-bin/draft/batchget"
_PUBLISHED_BATCHGET_URL = "https://api.weixin.qq.com/cgi-bin/freepublish/batchget"


class TokenProvider(Protocol):
    """The retry behavior required from TokenManager."""

    def get_token(self) -> str: ...

    def with_token_retry(self, call: Any) -> Any: ...


class OfficialReadOnlyChecker:
    """Check token, IP allowlist, and two list permissions without changing data."""

    def __init__(self, client: httpx.Client, tokens: TokenProvider) -> None:
        self._client = client
        self._tokens = tokens

    def run(self) -> dict[str, object]:
        """Perform only stable-token and batch-list reads."""
        checks: dict[str, str] = {}
        try:
            self._tokens.get_token()
        except WxcliError as error:
            check = "ip_allowlist" if error.details.get("errcode") in {40164, 61004} else "stable_token"
            checks[check] = "fail"
            raise self._with_checks(error, check, checks) from error
        checks.update({"stable_token": "pass", "ip_allowlist": "pass"})
        try:
            draft = self._run_batchget("draft_batchget", _DRAFT_BATCHGET_URL)
            checks["draft_batchget"] = "pass"
            published = self._run_batchget("freepublish_batchget", _PUBLISHED_BATCHGET_URL)
            checks["freepublish_batchget"] = "pass"
        except WxcliError as error:
            check = str(error.details.get("check", "official_api"))
            checks[check] = "fail"
            raise self._with_checks(error, check, checks) from error
        return {
            "stable_token": checks["stable_token"],
            "ip_allowlist": checks["ip_allowlist"],
            "draft_batchget": draft,
            "freepublish_batchget": published,
        }

    @staticmethod
    def _with_checks(error: WxcliError, check: str, checks: dict[str, str]) -> WxcliError:
        return WxcliError(
            error.code,
            error.message,
            {**error.details, "check": check, "checks": checks},
        )

    def _run_batchget(self, check: str, url: str) -> dict[str, int]:
        try:
            result = self._tokens.with_token_retry(lambda token: self._batchget(url, token))
        except WxcliError as error:
            raise WxcliError(
                error.code,
                error.message,
                {**error.details, "check": check},
            ) from error
        if not isinstance(result, dict):
            raise WxcliError(ErrorCode.GENERAL_ERROR, "The read-only API check returned invalid data.")
        return result

    def _batchget(self, url: str, token: str) -> dict[str, int]:
        try:
            response = self._client.post(
                url,
                params={"access_token": token},
                json={"offset": 0, "count": 1, "no_content": 1},
            )
        except httpx.HTTPError as error:
            raise WxcliError(
                ErrorCode.NETWORK_ERROR, "The Official Account API could not be reached."
            ) from error
        try:
            payload = response.json()
        except ValueError as error:
            raise WxcliError(
                ErrorCode.NETWORK_ERROR, "The Official Account API returned invalid JSON."
            ) from error
        if not isinstance(payload, dict):
            raise WxcliError(
                ErrorCode.NETWORK_ERROR, "The Official Account API returned invalid JSON."
            )
        raise_for_official_error(payload)
        if response.status_code != 200:
            raise WxcliError(ErrorCode.NETWORK_ERROR, "The Official Account API returned an HTTP error.")
        total_count = payload.get("total_count", 0)
        item_count = payload.get("item_count", 0)
        return {
            "total_count": total_count if isinstance(total_count, int) else 0,
            "item_count": item_count if isinstance(item_count, int) else 0,
        }
