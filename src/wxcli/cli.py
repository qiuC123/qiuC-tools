"""Top-level command-line interface for wxcli."""

from __future__ import annotations

import sys

import typer
from typer._click.exceptions import Exit, UsageError

from wxcli import __version__
from wxcli.errors import ExitCode, InputError
from wxcli.output import Output, configure_utf8_streams

app = typer.Typer(
    name="wxcli",
    help="Windows-only, read-only WeChat Official Account CLI.",
    no_args_is_help=False,
    add_completion=False,
)


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


def main() -> None:
    """Run the command-line application."""
    configure_utf8_streams()
    json_mode = "--json" in sys.argv[1:]
    try:
        app(standalone_mode=False)
    except UsageError as error:
        Output(json_mode=json_mode).error(InputError("Invalid command-line arguments."))
        raise SystemExit(ExitCode.INPUT) from error
    except Exit as error:
        raise SystemExit(error.exit_code) from error


if __name__ == "__main__":
    main()
