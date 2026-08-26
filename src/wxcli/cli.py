"""Top-level command-line interface for wxcli."""

from __future__ import annotations

import typer

from wxcli import __version__

app = typer.Typer(
    name="wxcli",
    help="Windows-only, read-only WeChat Official Account CLI.",
    no_args_is_help=False,
    add_completion=False,
)


def version_callback(value: bool) -> None:
    """Print the version without requiring a command."""
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def root(
    context: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        callback=version_callback,
        is_eager=True,
        help="Show the wxcli version and exit.",
    ),
) -> None:
    """Read WeChat Official Account content without modifying it."""
    if context.invoked_subcommand is None and not version:
        typer.echo(context.get_help())


def main() -> None:
    """Run the command-line application."""
    app()


if __name__ == "__main__":
    main()
