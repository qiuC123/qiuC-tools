"""Strict public URL identity and query fingerprint helpers."""

from __future__ import annotations

import hashlib
import json
from urllib.parse import parse_qs, urlsplit

from wxcli.discovery.models import DiscoveryRequest
from wxcli.errors import ValidationError
from wxcli.public_url import validate_public_url


def article_identity(value: str) -> tuple[str, str]:
    """Return the strict fetch URL and stable article identity."""
    normalized = validate_public_url(value)
    parsed = urlsplit(normalized)
    if parsed.path != "/s":
        token = parsed.path.removeprefix("/s/")
        return normalized, f"token:{token}"
    values = parse_qs(parsed.query)
    biz = _one(values, "__biz")
    mid = _one(values, "mid")
    idx = _one(values, "idx", default="1")
    return normalized, f"message:{biz}:{mid}:{idx}"


def query_fingerprint(request: DiscoveryRequest, provider: str = "brave") -> str:
    """Hash only fields that define discovery results, never credentials or run controls."""
    payload = {
        "schema_version": request.schema_version,
        "provider": provider,
        "query": " ".join(request.query.split()).casefold(),
        "companies": sorted(value.casefold() for value in request.companies),
        "expected_accounts": sorted(
            (
                account.biz_id or "",
                tuple(sorted(name.casefold() for name in account.display_names)),
            )
            for account in request.expected_accounts
        ),
        "published_after": request.published_after.isoformat() if request.published_after else None,
        "published_before": request.published_before.isoformat() if request.published_before else None,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _one(values: dict[str, list[str]], key: str, default: str | None = None) -> str:
    items = values.get(key)
    if items is None and default is not None:
        return default
    if items is None or len(items) != 1 or not items[0]:
        raise ValidationError(f"The article URL requires one non-empty {key} value.")
    return items[0]
