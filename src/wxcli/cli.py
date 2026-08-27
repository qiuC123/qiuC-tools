"""Top-level command-line interface for wxcli."""

from __future__ import annotations

import sys
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import typer
from typer._click.exceptions import Exit, UsageError

from wxcli import __version__
from wxcli.auth import AccessTokenStore, AppIdStore, SecretStore, TokenManager, default_backend
from wxcli.cache import ArticleCache
from wxcli.browser import BrowserProfile
from wxcli.doctor import Doctor
from wxcli.draft_import import WordDraftImporter
from wxcli.draft_import import PreparedDraft
from wxcli.draft_update import DraftUpdatePlanner
from wxcli.errors import ErrorCode, ExitCode, InputError, WxcliError
from wxcli.official_check import OfficialReadOnlyChecker
from wxcli.official_draft import OfficialDraftWriter
from wxcli.output import Output, configure_utf8_streams, is_interactive
from wxcli.providers.local import LocalFileProvider
from wxcli.providers.http import PublicHttpProvider
from wxcli.providers.chrome import ChromeProvider
from wxcli.providers.official import OfficialAccountProvider
from wxcli.providers.chrome import CHROME_PATH

app = typer.Typer(
    name="wxcli",
    help="Windows WeChat CLI for content reading and explicitly confirmed draft creation.",
    no_args_is_help=False,
    add_completion=False,
)
article_app = typer.Typer(help="Read individual articles without modifying them.")
cache_app = typer.Typer(help="Manage successful public-article cache entries.")
browser_app = typer.Typer(help="Use the dedicated visible Chrome profile.")
auth_app = typer.Typer(help="Configure and test Official Account read-only access.")
account_app = typer.Typer(help="Read drafts and published Official Account messages.")
draft_app = typer.Typer(help="Read, preview, back up, compare, or safely change drafts.")
published_app = typer.Typer(help="Read published messages by article_id.")
app.add_typer(article_app, name="article")
app.add_typer(cache_app, name="cache")
app.add_typer(browser_app, name="browser")
app.add_typer(auth_app, name="auth")
app.add_typer(account_app, name="account")
account_app.add_typer(draft_app, name="draft")
account_app.add_typer(published_app, name="published")


def default_cache() -> ArticleCache:
    """Return the per-user runtime cache without storing credentials there."""
    root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return ArticleCache(root / "wxcli" / "cache")


def default_browser_profile() -> BrowserProfile:
    """Return wxcli's independent profile and local status paths."""
    root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "wxcli"
    return BrowserProfile(root / "chrome-profile", root / "browser-state.json")


def default_runtime_root() -> Path:
    """Return the per-user directory for non-secret config and state."""
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "wxcli"


def default_auth_stores() -> tuple[AppIdStore, SecretStore, AccessTokenStore]:
    """Build auth stores without reading or exposing any credential values."""
    root = default_runtime_root()
    backend = default_backend()
    return (
        AppIdStore(root / "config.json"),
        SecretStore(backend),
        AccessTokenStore(backend, root / "token-state.json"),
    )


def official_provider(client: httpx.Client) -> OfficialAccountProvider:
    """Build the official provider from configured local stores."""
    return OfficialAccountProvider(client, official_token_manager(client))


def official_token_manager(client: httpx.Client) -> TokenManager:
    """Build the token manager without exposing configured credential values."""
    appids, secrets, tokens = default_auth_stores()
    appid = appids.get()
    if not appid:
        raise WxcliError(ErrorCode.AUTHENTICATION_ERROR, "The AppID is not configured.")
    return TokenManager(client, appid, secrets, tokens)


def default_doctor() -> Doctor:
    """Build the offline-by-default diagnostic runner."""
    appids, secrets, tokens = default_auth_stores()
    return Doctor(
        runtime_root=default_runtime_root(),
        chrome_path=CHROME_PATH,
        browser_profile=default_browser_profile(),
        appids=appids,
        secrets=secrets,
        tokens=tokens,
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
    """Read WeChat content or explicitly create a new, unpublished draft."""
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


@article_app.command("get")
def article_from_public_url(
    context: typer.Context,
    url: str = typer.Argument(..., help="Supported public WeChat article URL."),
    no_cache: bool = typer.Option(False, "--no-cache", help="Do not read or write cache."),
    browser: bool = typer.Option(False, "--browser", help="Open visible Chrome for this request."),
) -> None:
    """Read a supported public article URL through HTTP."""
    output = context.find_root().obj
    if not isinstance(output, Output):
        raise WxcliError(ErrorCode.GENERAL_ERROR, "The command output is unavailable.")
    if browser:
        output.success(
            ChromeProvider(default_browser_profile(), cache=default_cache()).get(
                url, no_cache=no_cache
            )
        )
        return
    with httpx.Client(timeout=30.0) as client:
        output.success(PublicHttpProvider(client, default_cache()).get(url, no_cache=no_cache))


@cache_app.command("clear")
def clear_cache(context: typer.Context) -> None:
    """Delete only successful public article cache records."""
    output = context.find_root().obj
    if not isinstance(output, Output):
        raise WxcliError(ErrorCode.GENERAL_ERROR, "The command output is unavailable.")
    output.success({"cleared": default_cache().clear()})


@browser_app.command("login")
def browser_login(context: typer.Context) -> None:
    """Open the dedicated visible Chrome profile for manual WeChat login."""
    output = context.find_root().obj
    if not isinstance(output, Output):
        raise WxcliError(ErrorCode.GENERAL_ERROR, "The command output is unavailable.")
    output.diagnostic("Chrome is opening with the wxcli-only profile.")
    ChromeProvider(default_browser_profile()).open_login()
    output.success({"opened": True})


@browser_app.command("status")
def browser_status(context: typer.Context) -> None:
    """Report local profile facts without starting Chrome."""
    output = context.find_root().obj
    if not isinstance(output, Output):
        raise WxcliError(ErrorCode.GENERAL_ERROR, "The command output is unavailable.")
    status = default_browser_profile().status()
    output.success({"profile_exists": status.profile_exists, "last_verified_at": status.last_verified_at})


@browser_app.command("clear")
def browser_clear(context: typer.Context) -> None:
    """Delete only wxcli's dedicated Chrome profile and local status record."""
    output = context.find_root().obj
    if not isinstance(output, Output):
        raise WxcliError(ErrorCode.GENERAL_ERROR, "The command output is unavailable.")
    default_browser_profile().clear()
    output.success({"cleared": True})


@auth_app.command("configure")
def auth_configure(context: typer.Context) -> None:
    """Interactively store AppID and AppSecret in their approved locations."""
    output = context.find_root().obj
    if not isinstance(output, Output):
        raise WxcliError(ErrorCode.GENERAL_ERROR, "The command output is unavailable.")
    if output.json_mode or not is_interactive():
        raise InputError("Credential setup requires an interactive terminal.")
    appid = typer.prompt("AppID", err=True)
    secret = typer.prompt("AppSecret", hide_input=True, confirmation_prompt=True, err=True)
    appids, secrets, tokens = default_auth_stores()
    appids.put(appid)
    secrets.set_app_secret(secret)
    tokens.clear()
    output.success({"configured": True})


@auth_app.command("status")
def auth_status(context: typer.Context) -> None:
    """Report only whether credentials exist; never print their values."""
    output = context.find_root().obj
    if not isinstance(output, Output):
        raise WxcliError(ErrorCode.GENERAL_ERROR, "The command output is unavailable.")
    appids, secrets, _ = default_auth_stores()
    output.success(
        {"appid_configured": appids.get() is not None, "appsecret_configured": secrets.get_app_secret() is not None}
    )


@auth_app.command("test")
def auth_test(
    context: typer.Context,
    allow_live_api: bool = typer.Option(
        False,
        "--allow-live-api",
        help="Explicitly authorize real read-only WeChat API requests.",
    ),
) -> None:
    """Check stable token and read-only list permissions without forcing refresh."""
    output = context.find_root().obj
    if not isinstance(output, Output):
        raise WxcliError(ErrorCode.GENERAL_ERROR, "The command output is unavailable.")
    if not allow_live_api:
        raise InputError("Real API checks require --allow-live-api.")
    appids, secrets, tokens = default_auth_stores()
    appid = appids.get()
    if not appid:
        raise WxcliError(ErrorCode.AUTHENTICATION_ERROR, "The AppID is not configured.")
    with httpx.Client(timeout=30.0) as client:
        manager = TokenManager(client, appid, secrets, tokens)
        output.success(OfficialReadOnlyChecker(client, manager).run())


@draft_app.command("list")
def account_draft_list(
    context: typer.Context,
    offset: int = typer.Option(0, min=0, help="Zero-based result offset."),
    count: int = typer.Option(20, min=1, max=20, help="Number of messages, from 1 to 20."),
) -> None:
    """List draft messages, preserving every article and its index."""
    output = context.find_root().obj
    if not isinstance(output, Output):
        raise WxcliError(ErrorCode.GENERAL_ERROR, "The command output is unavailable.")
    with httpx.Client(timeout=30.0) as client:
        output.success(official_provider(client).list_drafts(offset=offset, count=count))


@draft_app.command("get")
def account_draft_get(
    context: typer.Context,
    media_id: str = typer.Argument(..., help="Exact draft media_id."),
) -> None:
    """Get one draft by its exact media_id."""
    output = context.find_root().obj
    if not isinstance(output, Output):
        raise WxcliError(ErrorCode.GENERAL_ERROR, "The command output is unavailable.")
    with httpx.Client(timeout=30.0) as client:
        output.success(official_provider(client).get_draft(media_id))


@draft_app.command("import-word")
def account_draft_import_word(
    context: typer.Context,
    path: Path = typer.Argument(..., help="Word .docx article to map without rewriting text."),
    cover: Path = typer.Option(..., "--cover", help="JPG or PNG cover image."),
    output_dir: Path | None = typer.Option(
        None,
        "--output",
        help="Empty directory for the local HTML preview and prepared images.",
    ),
    author: str | None = typer.Option(None, "--author", help="Optional author, up to 16 characters."),
    digest: str | None = typer.Option(None, "--digest", help="Optional digest, up to 120 characters."),
    confirm: bool = typer.Option(
        False,
        "--confirm",
        help="Explicitly upload images and create one new draft; never publishes it.",
    ),
) -> None:
    """Map Word text and images into a new Official Account draft without publishing."""
    output = context.find_root().obj
    if not isinstance(output, Output):
        raise WxcliError(ErrorCode.GENERAL_ERROR, "The command output is unavailable.")
    importer = WordDraftImporter()
    if not confirm:
        destination = output_dir or path.parent / f"{path.stem}_wxcli_preview"
        prepared = importer.prepare(path, cover, destination, author=author, digest=digest)
        output.success(prepared.preview)
        return

    if output_dir is not None:
        package = output_dir / "package.json"
        prepared = (
            PreparedDraft.load(output_dir)
            if package.is_file()
            else importer.prepare(path, cover, output_dir, author=author, digest=digest)
        )
        output.diagnostic(
            f"Creating one unpublished draft: {prepared.title} "
            f"({len(prepared.images)} body images)."
        )
        with httpx.Client(timeout=60.0) as client:
            output.success(
                OfficialDraftWriter(
                    client,
                    official_token_manager(client),
                    default_runtime_root() / "upload-checkpoints",
                ).create(prepared)
            )
        return

    with TemporaryDirectory(prefix="wxcli-draft-") as temporary:
        prepared = importer.prepare(
            path,
            cover,
            Path(temporary),
            author=author,
            digest=digest,
        )
        output.diagnostic(
            f"Creating one unpublished draft: {prepared.title} "
            f"({len(prepared.images)} body images)."
        )
        with httpx.Client(timeout=60.0) as client:
            output.success(
                OfficialDraftWriter(
                    client,
                    official_token_manager(client),
                    default_runtime_root() / "upload-checkpoints",
                ).create(prepared)
            )


@draft_app.command("backup")
def account_draft_backup(
    context: typer.Context,
    media_id: str = typer.Argument(..., help="Exact draft media_id."),
    output_path: Path = typer.Option(..., "--output", help="New JSON backup file."),
) -> None:
    """Save an exact local draft snapshot without modifying WeChat."""
    output = context.find_root().obj
    if not isinstance(output, Output):
        raise WxcliError(ErrorCode.GENERAL_ERROR, "The command output is unavailable.")
    with httpx.Client(timeout=30.0) as client:
        writer = OfficialDraftWriter(
            client,
            official_token_manager(client),
            default_runtime_root() / "upload-checkpoints",
        )
        output.success(DraftUpdatePlanner(writer).backup(media_id, output_path))


@draft_app.command("diff")
def account_draft_diff(
    context: typer.Context,
    media_id: str = typer.Argument(..., help="Exact draft media_id."),
    path: Path = typer.Argument(..., help="Replacement Word .docx article."),
    cover: Path = typer.Option(..., "--cover", help="JPG or PNG cover image."),
    output_dir: Path = typer.Option(..., "--output", help="Empty directory for the update plan."),
    index: int = typer.Option(0, "--index", min=0, help="Zero-based article index."),
    author: str | None = typer.Option(None, "--author"),
    digest: str | None = typer.Option(None, "--digest"),
) -> None:
    """Back up and compare one draft article; performs no remote write."""
    output = context.find_root().obj
    if not isinstance(output, Output):
        raise WxcliError(ErrorCode.GENERAL_ERROR, "The command output is unavailable.")
    with httpx.Client(timeout=30.0) as client:
        writer = OfficialDraftWriter(
            client,
            official_token_manager(client),
            default_runtime_root() / "upload-checkpoints",
        )
        output.success(
            DraftUpdatePlanner(writer).plan(
                media_id,
                index,
                path,
                cover,
                output_dir,
                author=author,
                digest=digest,
            )
        )


@draft_app.command("update")
def account_draft_update(
    context: typer.Context,
    plan_dir: Path = typer.Argument(..., help="Existing directory produced by draft diff."),
    confirm: bool = typer.Option(False, "--confirm", help="Apply this exact update plan."),
) -> None:
    """Apply one frozen update plan after rechecking the remote fingerprint."""
    output = context.find_root().obj
    if not isinstance(output, Output):
        raise WxcliError(ErrorCode.GENERAL_ERROR, "The command output is unavailable.")
    if not confirm:
        raise InputError("Applying a draft update requires --confirm.")
    with httpx.Client(timeout=60.0) as client:
        writer = OfficialDraftWriter(
            client,
            official_token_manager(client),
            default_runtime_root() / "upload-checkpoints",
        )
        output.success(DraftUpdatePlanner(writer).apply(plan_dir, confirmed=True))


@published_app.command("list")
def account_published_list(
    context: typer.Context,
    offset: int = typer.Option(0, min=0, help="Zero-based result offset."),
    count: int = typer.Option(20, min=1, max=20, help="Number of messages, from 1 to 20."),
) -> None:
    """List published messages, preserving every article and its index."""
    output = context.find_root().obj
    if not isinstance(output, Output):
        raise WxcliError(ErrorCode.GENERAL_ERROR, "The command output is unavailable.")
    with httpx.Client(timeout=30.0) as client:
        output.success(official_provider(client).list_published(offset=offset, count=count))


@published_app.command("get")
def account_published_get(
    context: typer.Context,
    article_id: str = typer.Argument(..., help="Exact published article_id."),
) -> None:
    """Get one published message by its exact article_id."""
    output = context.find_root().obj
    if not isinstance(output, Output):
        raise WxcliError(ErrorCode.GENERAL_ERROR, "The command output is unavailable.")
    with httpx.Client(timeout=30.0) as client:
        output.success(official_provider(client).get_published(article_id))


@app.command("doctor")
def doctor_command(
    context: typer.Context,
    allow_live_api: bool = typer.Option(
        False,
        "--allow-live-api",
        help="Explicitly authorize real network and read-only account checks.",
    ),
) -> None:
    """Diagnose local prerequisites; skip real network and account checks by default."""
    output = context.find_root().obj
    if not isinstance(output, Output):
        raise WxcliError(ErrorCode.GENERAL_ERROR, "The command output is unavailable.")
    report = default_doctor().run(allow_live_api=allow_live_api)
    output.success(report)
    if report.overall == "fail":
        raise typer.Exit(ExitCode.GENERAL)


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
