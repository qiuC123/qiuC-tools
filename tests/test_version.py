"""Tests for the initial command-line contract."""

import json
import sys

import pytest
from typer.testing import CliRunner

from wxcli import __version__
from wxcli.cli import app, main


def test_version_option_reports_package_version() -> None:
    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout == f"{__version__}\n"


def test_no_arguments_shows_help() -> None:
    result = CliRunner().invoke(app, [])

    assert result.exit_code == 0
    assert "read-only" in result.stdout


def test_json_version_is_a_single_json_document() -> None:
    result = CliRunner().invoke(app, ["--json", "--version"])

    assert result.exit_code == 0
    assert result.stdout == f'{{"ok":true,"data":{{"version":"{__version__}"}}}}\n'


def test_unknown_option_uses_input_exit_code() -> None:
    result = CliRunner().invoke(app, ["--not-an-option"])

    assert result.exit_code == 2


def test_json_invalid_option_is_a_single_json_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["wxcli", "--json", "--not-an-option"])

    with pytest.raises(SystemExit) as raised:
        main()

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "ok": False,
        "error": {
            "code": "INVALID_ARGUMENT",
            "message": "Invalid command-line arguments.",
            "details": {},
        },
    }
