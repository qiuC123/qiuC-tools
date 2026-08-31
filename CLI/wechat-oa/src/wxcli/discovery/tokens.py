"""Opaque, query-bound pagination cursors and incremental checkpoints."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal, TypedDict

from wxcli.errors import ValidationError

_TOKEN_NAMESPACE = b"wxcli-discovery-v1"


class CursorPayload(TypedDict):
    schema: Literal["1"]
    kind: Literal["cursor"]
    provider: str
    query_fingerprint: str
    offset: int


class CheckpointPayload(TypedDict):
    schema: Literal["1"]
    kind: Literal["checkpoint"]
    provider: str
    query_fingerprint: str
    observed_before: str


def encode_cursor(provider: str, fingerprint: str, offset: int) -> str:
    return _encode(
        CursorPayload(
            schema="1",
            kind="cursor",
            provider=provider,
            query_fingerprint=fingerprint,
            offset=offset,
        )
    )


def decode_cursor(value: str, provider: str, fingerprint: str) -> int:
    payload = _decode(value, "cursor", provider, fingerprint)
    offset = payload.get("offset")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValidationError("The discovery cursor is invalid.")
    return offset


def encode_checkpoint(provider: str, fingerprint: str, observed_before: datetime) -> str:
    return _encode(
        CheckpointPayload(
            schema="1",
            kind="checkpoint",
            provider=provider,
            query_fingerprint=fingerprint,
            observed_before=observed_before.astimezone(UTC).isoformat(),
        )
    )


def decode_checkpoint(value: str, provider: str, fingerprint: str) -> datetime:
    payload = _decode(value, "checkpoint", provider, fingerprint)
    raw = payload.get("observed_before")
    if not isinstance(raw, str):
        raise ValidationError("The discovery checkpoint is invalid.")
    try:
        result = datetime.fromisoformat(raw)
    except ValueError as error:
        raise ValidationError("The discovery checkpoint is invalid.") from error
    if result.tzinfo is None:
        raise ValidationError("The discovery checkpoint is invalid.")
    return result.astimezone(UTC)


def _encode(payload: Mapping[str, Any]) -> str:
    body = json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
    checksum = hashlib.sha256(_TOKEN_NAMESPACE + body).digest()[:16]
    return base64.urlsafe_b64encode(body + checksum).decode("ascii").rstrip("=")


def _decode(value: str, kind: str, provider: str, fingerprint: str) -> dict[str, Any]:
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
        body, checksum = decoded[:-16], decoded[-16:]
        expected = hashlib.sha256(_TOKEN_NAMESPACE + body).digest()[:16]
        if len(checksum) != 16 or checksum != expected:
            raise ValueError
        payload = json.loads(body)
    except (ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise ValidationError(f"The discovery {kind} is invalid.") from error
    if not isinstance(payload, dict) or payload.get("schema") != "1":
        raise ValidationError(f"The discovery {kind} schema is unsupported.")
    if payload.get("kind") != kind:
        raise ValidationError(f"The discovery {kind} is invalid.")
    if payload.get("provider") != provider:
        raise ValidationError(f"The discovery {kind} belongs to a different provider.")
    if payload.get("query_fingerprint") != fingerprint:
        raise ValidationError(f"The discovery {kind} belongs to a different query.")
    return payload
