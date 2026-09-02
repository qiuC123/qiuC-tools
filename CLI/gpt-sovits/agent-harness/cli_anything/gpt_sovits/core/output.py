from __future__ import annotations

import json
from typing import Any

import click

from cli_anything.gpt_sovits.core.errors import CLIError


def envelope(command: str, data: Any = None, warnings: list[str] | None = None, error: dict | None = None) -> dict:
    return {
        "ok": error is None,
        "command": command,
        "data": data if error is None else None,
        "warnings": warnings or [],
        "error": error,
    }


def emit(command: str, data: Any, use_json: bool, warnings: list[str] | None = None) -> None:
    result = envelope(command, data=data, warnings=warnings)
    if use_json:
        click.echo(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if isinstance(data, dict):
        for key, value in data.items():
            click.echo(f"{key}: {value}")
    elif isinstance(data, list):
        for value in data:
            click.echo(value)
    else:
        click.echo(data)
    for warning in warnings or []:
        click.echo(f"警告: {warning}", err=True)


def fail(command: str, exc: Exception, use_json: bool) -> None:
    known = exc if isinstance(exc, CLIError) else CLIError("unexpected_error", str(exc))
    if use_json:
        click.echo(json.dumps(envelope(command, error=known.as_dict()), ensure_ascii=False, indent=2))
    else:
        click.echo(f"错误 [{known.code}]: {known.message}", err=True)
        if known.details:
            click.echo(json.dumps(known.details, ensure_ascii=False, indent=2), err=True)
    raise click.exceptions.Exit(1)
