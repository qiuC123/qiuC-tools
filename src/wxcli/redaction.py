"""Central defensive redaction for values that must never reach output."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

_REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = {
    "appsecret",
    "app_secret",
    "access_token",
    "token",
    "cookie",
    "cookies",
    "authorization",
}
_TEXT_SECRET = re.compile(
    r"(?i)\b(appsecret|app_secret|access_token|token|cookie|authorization)\b"
    r"(\s*[:=]\s*)([^\r\n]*)"
)


def redact(value: Any) -> Any:
    """Recursively replace values stored under known-sensitive keys."""
    if isinstance(value, BaseModel):
        return redact(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {
            str(key): _REDACTED if str(key).lower() in _SENSITIVE_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value


def redact_text(message: str) -> str:
    """Remove assignment-like credential values from diagnostic text."""
    return _TEXT_SECRET.sub(lambda match: f"{match.group(1)}{match.group(2)}{_REDACTED}", message)
