"""Top-level command-line interface for wxcli."""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from typer._click.exceptions import Exit, UsageError

from wxcli import __version__
from wxcli.errors import ErrorCode, ExitCode, InputError, WxcliError
from wxcli.output import Output, configure_utf8_streams
from wxcli.providers.local import LocalFileProvider

app = typer.Typer(
    name="wxcli",
    help="Windows-only, read-only WeChat Official Account CLI.",
    no_args_is_help=False,
    add_completion=False,
)
article_app = typer.Typer(help="Read individual articles without modifying them.")
app.add_typer(article_app, name="article")


@app.callback(invoke_without_command=True)
def root(
    context: typer.Context,
    json_mode: bool = typer.Option(
        False,
        "--json",
        help="Write exactly one UTF-8 JSON result to standard output.",
    ),
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the wxcli version and exit.",
    ),
) -> None:
    """Read WeChat Official Account content without modifying it."""
    output = Output(json_mode=json_mode)
    context.obj = output
    if version:
        output.success({"version": __version__} if json_mode else __version__)
        raise typer.Exit()
    if context.invoked_subcommand is None:
        if json_mode:
            output.success({"help": context.get_help()})
            return
        typer.echo(context.get_help())


@article_app.command("local")
def article_from_local_file(
    context: typer.Context,
    path: Path = typer.Argument(..., help="UTF-8 HTML or Markdown file to read."),
) -> None:
    """Read a local HTML or Markdown file as an Article."""
    output = context.find_root().obj
    if not isinstance(output, Output):
        raise WxcliError(ErrorCode.GENERAL_ERROR, "The command output is unavailable.")
    output.success(LocalFileProvider().get(path))


def main() -> None:
    """Run the command-line application."""
    configure_utf8_streams()
    json_mode = "--json" in sys.argv[1:]
    try:
        app(standalone_mode=False)
    except UsageError as error:
        Output(json_mode=json_mode).error(InputError("Invalid command-line arguments."))
        raise SystemExit(ExitCode.INPUT) from error
    except WxcliError as error:
        Output(json_mode=json_mode).error(error)
        raise SystemExit(error.exit_code) from error
    except Exit as error:
        raise SystemExit(error.exit_code) from error


if __name__ == "__main__":
    main()
