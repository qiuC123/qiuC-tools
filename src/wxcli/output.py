"""UTF-8-safe output helpers for text and one-document JSON responses."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TextIO

from pydantic import BaseModel

from wxcli.errors import WxcliError
from wxcli.redaction import redact, redact_text


def configure_utf8_streams() -> None:
    """Use UTF-8 for console streams when the host stream supports reconfigure."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="strict")


def is_interactive() -> bool:
    """Return whether both input and diagnostic streams are attached to a TTY."""
    return sys.stdin.isatty() and sys.stderr.isatty()


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def _json_default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


@dataclass(frozen=True, slots=True)
class Output:
    """A command-scoped writer that keeps JSON stdout machine-readable."""

    json_mode: bool
    stdout: TextIO = field(default_factory=lambda: sys.stdout)
    stderr: TextIO = field(default_factory=lambda: sys.stderr)

    def success(self, data: Any) -> None:
        """Write a successful result to standard output."""
        if self.json_mode:
            self.stdout.write(
                json.dumps(
                    {"ok": True, "data": redact(_json_value(data))},
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=_json_default,
                )
                + "\n"
            )
            return
        self.stdout.write(f"{data}\n")

    def error(self, error: WxcliError) -> None:
        """Write a structured error to stdout or a concise diagnostic to stderr."""
        if self.json_mode:
            payload = {
                "ok": False,
                "error": {
                    "code": error.code,
                    "message": redact_text(error.message),
                    "details": redact(error.details),
                },
            }
            self.stdout.write(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=_json_default)
                + "\n"
            )
            return
        self.stderr.write(f"{error.code}: {redact_text(error.message)}\n")

    def diagnostic(self, message: str) -> None:
        """Write non-result information only to standard error."""
        self.stderr.write(f"{redact_text(message)}\n")
