"""Tests for the initial command-line contract."""

from typer.testing import CliRunner

from wxcli import __version__
from wxcli.cli import app


def test_version_option_reports_package_version() -> None:
    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout == f"{__version__}\n"


def test_no_arguments_shows_help() -> None:
    result = CliRunner().invoke(app, [])

    assert result.exit_code == 0
    assert "read-only" in result.stdout
