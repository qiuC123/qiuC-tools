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
_EXA_QUERY_LIMIT = 2_000
_EXA_SYSTEM_PROMPT = (
    "Prioritize direct articles published by the requested WeChat Official Account "
    "or company that match the complete topic and approximate publication window. "
    "Deprioritize other organizations, aggregators, reposts, interview stories, and "
    "generic roundups. Treat dates as soft retrieval guidance, not hard filters."
)


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
            "additionalQueries": _build_additional_queries(request),
            "includeDomains": ["mp.weixin.qq.com"],
            "numResults": min(max(count, 1), _EXA_RESULT_LIMIT),
            "type": "deep",
            "moderation": True,
            "systemPrompt": _EXA_SYSTEM_PROMPT,
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
    subjects = _subject_terms(request)
    topic = " ".join(_unique_terms([request.query, *_query_expansions(request.query)]))
    if subjects:
        lead = f"优先查找由“{'、'.join(subjects)}”微信公众号直接发布的文章"
    else:
        lead = "查找微信公众号直接发布的文章"
    clauses = [lead, f"主题：{topic}"]
    if window := _soft_publication_window(request):
        clauses.append(window)
    return _bounded_query("。".join(clauses) + "。")


def _build_additional_queries(request: DiscoveryRequest) -> list[str]:
    subjects = _subject_terms(request)
    expansions = _query_expansions(request.query)
    numeric_terms = re.findall(r"(?<!\d)\d{4}(?!\d)", request.query)
    candidates: list[str] = [" ".join([*subjects, request.query])]
    if expansions:
        candidates.append(" ".join([*subjects, *expansions]))
    if numeric_terms and any(term in request.query for term in ("秋招", "春招", "校招", "校园招聘")):
        candidates.append(
            " ".join([*subjects, *numeric_terms, "人才计划", "校招", "校园招聘"])
        )
    publication_years = _publication_years(request)
    if publication_years:
        candidates.append(
            " ".join(
                [*subjects, request.query, *(f"{year}年发布" for year in publication_years)]
            )
        )
    return [_bounded_query(value) for value in _unique_terms(candidates)][:10]


def _subject_terms(request: DiscoveryRequest) -> list[str]:
    terms = [*request.companies]
    for account in request.expected_accounts:
        terms.extend(account.display_names)
    return _unique_terms(terms)


def _unique_terms(terms: list[str]) -> list[str]:
    unique_terms: list[str] = []
    seen: set[str] = set()
    for term in terms:
        identity = " ".join(term.split()).casefold()
        if identity in seen:
            continue
        seen.add(identity)
        unique_terms.append(term)
    return unique_terms


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


def _soft_publication_window(request: DiscoveryRequest) -> str | None:
    if request.published_after and request.published_before:
        return (
            "发布时间约在 "
            f"{request.published_after.isoformat()} 至 {request.published_before.isoformat()}"
        )
    if request.published_after:
        return f"发布时间约在 {request.published_after.isoformat()} 之后"
    if request.published_before:
        return f"发布时间约在 {request.published_before.isoformat()} 之前"
    return None


def _publication_years(request: DiscoveryRequest) -> list[int]:
    years: list[int] = []
    for value in (request.published_after, request.published_before):
        if value is not None and value.year not in years:
            years.append(value.year)
    return years


def _bounded_query(value: str) -> str:
    return value[:_EXA_QUERY_LIMIT]


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
