"""Top-level command-line interface for WeChat OA."""

from __future__ import annotations

import sys
import os
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import typer
from pydantic import ValidationError as PydanticValidationError
from typer import Exit
from typer._click.exceptions import UsageError

from wxcli import __version__
from wxcli.auth import AccessTokenStore, AppIdStore, SecretStore, TokenManager, default_backend
from wxcli.cache import ArticleCache
from wxcli.browser import BrowserProfile
from wxcli.browser_policy import (
    BrowserDecision,
    BrowserFallbackPolicy,
    BrowserMode,
    BrowserPolicyStore,
    resolve_browser_decision,
)
from wxcli.doctor import Doctor
from wxcli.draft_import import WordDraftImporter
from wxcli.draft_import import PreparedDraft
from wxcli.draft_update import DraftUpdatePlanner
from wxcli.errors import ErrorCode, ExitCode, InputError, ValidationError, WxcliError
from wxcli.evidence import ArticleEvidence, EvidenceService
from wxcli.discovery.auth import DiscoverySecretStore
from wxcli.discovery.brave import BraveDiscoveryProvider
from wxcli.discovery.ingestion import CandidateIngestionService
from wxcli.discovery.media import (
    DiscoveryMediaAnalysisResult,
    DiscoveryMediaAnalyzer,
    DiscoveryMediaBudget,
)
from wxcli.discovery.models import (
    MAX_CANDIDATE_BATCH_BYTES,
    CandidateBatchRequest,
    CandidateIngestionResult,
    DiscoveryRequest,
    DiscoveryResult,
)
from wxcli.discovery.service import DiscoveryService, validate_discovery_tokens
from wxcli.discovery.store import DiscoveryStore
from wxcli.media import (
    ArticleMediaAnalyzer,
    ArticleMediaDownloader,
    MediaAnalysisConfiguration,
    MediaAnalysisLimits,
    MediaAnalysisResult,
    MediaCache,
    MediaDoctor,
    MediaDownloader,
    MediaEvidence,
)
from wxcli.official_check import OfficialReadOnlyChecker
from wxcli.official_draft import OfficialDraftWriter
from wxcli.output import Output, configure_utf8_streams, is_interactive
from wxcli.providers.local import LocalFileProvider
from wxcli.providers.http import PublicHttpProvider
from wxcli.providers.chrome import ChromeEvidenceService, ChromeProvider
from wxcli.providers.official import OfficialAccountProvider
from wxcli.providers.chrome import CHROME_PATH

app = typer.Typer(
    name="wechat-oa",
    help="WeChat Official Account CLI: read content and safely prepare unpublished drafts.",
    no_args_is_help=False,
    add_completion=False,
)
article_app = typer.Typer(help="Read individual articles without modifying them.")
cache_app = typer.Typer(help="Manage successful public-article cache entries.")
browser_app = typer.Typer(help="Use the dedicated visible Chrome profile.")
browser_policy_app = typer.Typer(help="Manage durable HTTP-to-Chrome fallback authorization.")
auth_app = typer.Typer(help="Configure and test Official Account read-only access.")
account_app = typer.Typer(help="Read drafts and published Official Account messages.")
draft_app = typer.Typer(help="Read, preview, back up, compare, or safely change drafts.")
published_app = typer.Typer(help="Read published messages by article_id.")
discovery_app = typer.Typer(help="Discover public WeChat articles through external search.")
discovery_auth_app = typer.Typer(help="Configure discovery-provider credentials.")
discovery_cache_app = typer.Typer(help="Manage discovery search state only.")
media_app = typer.Typer(help="Manage optional derived media-analysis state.")
media_cache_app = typer.Typer(help="Manage the dedicated public-image Media Cache.")
app.add_typer(article_app, name="article")
app.add_typer(cache_app, name="cache")
app.add_typer(browser_app, name="browser")
browser_app.add_typer(browser_policy_app, name="policy")
app.add_typer(auth_app, name="auth")
app.add_typer(account_app, name="account")
app.add_typer(discovery_app, name="discovery")
account_app.add_typer(draft_app, name="draft")
account_app.add_typer(published_app, name="published")
discovery_app.add_typer(discovery_auth_app, name="auth")
discovery_app.add_typer(discovery_cache_app, name="cache")
app.add_typer(media_app, name="media")
media_app.add_typer(media_cache_app, name="cache")


def default_cache() -> ArticleCache:
    """Return the per-user runtime cache without storing credentials there."""
    root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return ArticleCache(root / "wxcli" / "cache")


def default_browser_profile() -> BrowserProfile:
    """Return WeChat OA's independent profile and local status paths."""
    root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "wxcli"
    return BrowserProfile(root / "chrome-profile", root / "browser-state.json")


def default_browser_policy() -> BrowserPolicyStore:
    """Return the non-secret durable browser-fallback policy store."""
    return BrowserPolicyStore(default_runtime_root() / "browser-policy.json")


def default_runtime_root() -> Path:
    """Return the per-user directory for non-secret config and state."""
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "wxcli"


def default_discovery_store() -> DiscoveryStore:
    """Return the discovery-only cache, history, and checkpoint state."""
    return DiscoveryStore(default_runtime_root() / "discovery" / "state.sqlite3")


def default_media_cache() -> MediaCache:
    """Return the dedicated non-secret cache for validated public image bytes."""
    return MediaCache(default_runtime_root() / "media-cache")


def default_media_analysis_configuration() -> MediaAnalysisConfiguration:
    """Return invocation-owned defaults recorded in every Media Evidence result."""
    return MediaAnalysisConfiguration()


def default_media_doctor() -> MediaDoctor:
    """Return the credential-free local media capability probe."""
    return MediaDoctor()


def default_discovery_secrets() -> DiscoverySecretStore:
    """Return the discovery-specific Windows credential store facade."""
    return DiscoverySecretStore(default_backend())


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
        help="Show the WeChat OA version and exit.",
    ),
) -> None:
    """Discover or read WeChat content, or explicitly create an unpublished draft."""
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
    browser_fallback: bool = typer.Option(
        False,
        "--browser-fallback",
        help="Try HTTP first and use visible Chrome only after verification is required.",
    ),
    no_browser: bool = typer.Option(
        False,
        "--no-browser",
        help="Prohibit Chrome for this request even when durable fallback is enabled.",
    ),
    analyze_media: bool = typer.Option(
        False,
        "--analyze-media",
        help="Explicitly download eligible images and run local QR/OCR analysis.",
    ),
) -> None:
    """Read a supported public article URL through HTTP."""
    output = context.find_root().obj
    if not isinstance(output, Output):
        raise WxcliError(ErrorCode.GENERAL_ERROR, "The command output is unavailable.")
    decision = resolve_browser_decision(
        default_browser_policy(),
        browser=browser,
        browser_fallback=browser_fallback,
        no_browser=no_browser,
        browser_is_direct=True,
    )
    _diagnose_browser_warning(output, decision)
    if decision.mode is BrowserMode.DIRECT:
        chrome_provider = ChromeProvider(default_browser_profile(), cache=default_cache())
        if analyze_media:
            evidence = EvidenceService(chrome_provider).get(url, no_cache=no_cache)
            output.success(_media_analysis_result(evidence, no_cache=no_cache))
        else:
            output.success(chrome_provider.get(url, no_cache=no_cache))
        return
    with httpx.Client(timeout=30.0) as client:
        http_provider = PublicHttpProvider(client, default_cache())
        core_result: object
        try:
            if analyze_media:
                core_result = EvidenceService(http_provider).get(url, no_cache=no_cache)
            else:
                core_result = http_provider.get(url, no_cache=no_cache)
        except WxcliError as error:
            if error.code != ErrorCode.VERIFICATION_REQUIRED or not decision.allows_fallback:
                _attach_browser_warning(error, decision)
                raise
            chrome_provider = ChromeProvider(default_browser_profile(), cache=default_cache())
            if analyze_media:
                core_result = EvidenceService(chrome_provider).get(
                    url,
                    no_cache=no_cache,
                )
            else:
                core_result = chrome_provider.get(url, no_cache=no_cache)
        if analyze_media:
            assert isinstance(core_result, ArticleEvidence)
            output.success(_media_analysis_result(core_result, no_cache=no_cache))
        else:
            output.success(core_result)


@article_app.command("evidence")
def article_evidence(
    context: typer.Context,
    url: str = typer.Argument(..., help="Supported public WeChat article URL."),
    browser: bool = typer.Option(False, "--browser", help="Use visible Chrome for this request."),
    browser_fallback: bool = typer.Option(
        False,
        "--browser-fallback",
        help="Try HTTP first and use visible Chrome only after verification is required.",
    ),
    no_browser: bool = typer.Option(
        False,
        "--no-browser",
        help="Prohibit Chrome for this request even when durable fallback is enabled.",
    ),
    analyze_media: bool = typer.Option(
        False,
        "--analyze-media",
        help="Explicitly download eligible images and run local QR/OCR analysis.",
    ),
) -> None:
    """Read one real WeChat page and return versioned article evidence."""
    output = _output(context)
    decision = resolve_browser_decision(
        default_browser_policy(),
        browser=browser,
        browser_fallback=browser_fallback,
        no_browser=no_browser,
        browser_is_direct=True,
    )
    _diagnose_browser_warning(output, decision)
    if decision.mode is BrowserMode.DIRECT:
        chrome_provider = ChromeProvider(default_browser_profile(), cache=default_cache())
        evidence = EvidenceService(chrome_provider).get(url)
        output.success(
            _media_analysis_result(evidence) if analyze_media else evidence
        )
        return
    with httpx.Client(timeout=30.0) as client:
        http_provider = PublicHttpProvider(client, default_cache())
        try:
            evidence = EvidenceService(http_provider).get(url)
        except WxcliError as error:
            if error.code != ErrorCode.VERIFICATION_REQUIRED or not decision.allows_fallback:
                _attach_browser_warning(error, decision)
                raise
            evidence = EvidenceService(
                ChromeProvider(default_browser_profile(), cache=default_cache())
            ).get(url)
        output.success(_media_analysis_result(evidence) if analyze_media else evidence)


def _media_analysis_result(
    evidence: ArticleEvidence,
    *,
    no_cache: bool = False,
) -> MediaAnalysisResult:
    return MediaAnalysisResult(
        article_evidence=evidence,
        media_evidence=_media_evidence(evidence, no_cache=no_cache),
    )


def _media_evidence(
    evidence: ArticleEvidence,
    *,
    no_cache: bool = False,
    budget: DiscoveryMediaBudget | None = None,
    downloader: MediaDownloader | None = None,
    analyzer: ArticleMediaAnalyzer | None = None,
    cache: MediaCache | None = None,
) -> MediaEvidence:
    configuration = default_media_analysis_configuration()
    if budget is not None:
        configuration = configuration.model_copy(
            update={
                "limits": MediaAnalysisLimits.model_validate(
                    configuration.limits.model_dump()
                    | {
                        "max_article_images": budget.max_images,
                        "max_article_bytes": budget.max_bytes,
                        "max_ocr_characters_per_batch": budget.max_ocr_characters,
                    }
                )
            }
        )
    actual_downloader = downloader or MediaDownloader(
        max_bytes=configuration.limits.max_image_bytes,
        max_pixels=configuration.limits.max_image_pixels,
    )
    downloads = ArticleMediaDownloader(
        actual_downloader,
        cache=None if no_cache else (cache or default_media_cache()),
        limits=configuration.limits,
    ).download(evidence.article)
    actual_analyzer = analyzer or ArticleMediaAnalyzer()
    if budget is None:
        return actual_analyzer.analyze(
            source_content_sha256=evidence.content_sha256,
            downloads=downloads,
            configuration=configuration,
        )
    return actual_analyzer.analyze(
        source_content_sha256=evidence.content_sha256,
        downloads=downloads,
        configuration=configuration,
        ocr_character_budget=budget.max_ocr_characters,
    )


def _discovery_media_analysis_result(
    discovery_result: DiscoveryResult | CandidateIngestionResult,
) -> DiscoveryMediaAnalysisResult:
    configuration = default_media_analysis_configuration()
    downloader = MediaDownloader(
        max_bytes=configuration.limits.max_image_bytes,
        max_pixels=configuration.limits.max_image_pixels,
    )
    analyzer = ArticleMediaAnalyzer()
    cache = default_media_cache()
    return DiscoveryMediaAnalyzer(
        lambda evidence, budget: _media_evidence(
            evidence,
            budget=budget,
            downloader=downloader,
            analyzer=analyzer,
            cache=cache,
        )
    ).analyze(
        discovery_result
    )


@discovery_app.command("search")
def discovery_search(
    context: typer.Context,
    query: str | None = typer.Argument(None, help="Keywords used to discover WeChat articles."),
    input_path: str | None = typer.Option(None, "--input", help="Schema-v1 JSON file, or - for stdin."),
    company: list[str] | None = typer.Option(None, "--company", help="Repeatable company-name hint."),
    account: list[str] | None = typer.Option(None, "--account", help="Repeatable account-name hint."),
    published_after: str | None = typer.Option(None, "--published-after", help="Earliest YYYY-MM-DD."),
    published_before: str | None = typer.Option(None, "--published-before", help="Latest YYYY-MM-DD."),
    limit: int = typer.Option(50, "--limit", min=1, max=50),
    cursor: str | None = typer.Option(None, "--cursor", help="Opaque next-page cursor."),
    checkpoint: str | None = typer.Option(None, "--checkpoint", help="Opaque incremental checkpoint."),
    new_only: bool = typer.Option(False, "--new-only", help="Return only newly observed candidates."),
    hydrate: bool = typer.Option(False, "--hydrate", help="Read selected WeChat source pages."),
    analyze_media: bool = typer.Option(
        False,
        "--analyze-media",
        help="After hydration, explicitly download eligible images and run local QR/OCR analysis.",
    ),
    priority_hydrate: int = typer.Option(10, "--priority-hydrate", min=0, max=20),
    max_hydrate: int = typer.Option(20, "--max-hydrate", min=0, max=20),
    require_account_match: bool = typer.Option(False, "--require-account-match"),
    require_published_date: bool = typer.Option(False, "--require-published-date"),
    browser: bool = typer.Option(False, "--browser", help="Allow serial Chrome fallback after verification."),
    browser_fallback: bool = typer.Option(
        False,
        "--browser-fallback",
        help="Allow one-shot serial Chrome fallback after HTTP verification pages.",
    ),
    no_browser: bool = typer.Option(
        False,
        "--no-browser",
        help="Prohibit Chrome even when request JSON or durable policy allows it.",
    ),
) -> None:
    """Discover candidates and optionally hydrate selected WeChat articles."""
    output = _output(context)
    if input_path is not None and _has_explicit_discovery_search_options(context):
        raise InputError("--input cannot be combined with query or search options.")
    request = _discovery_request(
        query=query,
        input_path=input_path,
        companies=company or [],
        accounts=account or [],
        published_after=published_after,
        published_before=published_before,
        limit=limit,
        cursor=cursor,
        checkpoint=checkpoint,
        new_only=new_only,
        hydrate=hydrate,
        priority_hydrate=priority_hydrate,
        max_hydrate=max_hydrate,
        require_account_match=require_account_match,
        require_published_date=require_published_date,
        allow_browser=browser or browser_fallback,
    )
    if analyze_media and not request.hydrate:
        raise InputError("--analyze-media requires --hydrate.")
    decision = resolve_browser_decision(
        default_browser_policy(),
        browser=browser,
        browser_fallback=browser_fallback,
        no_browser=no_browser,
        request_allow_browser=request.allow_browser,
    )
    _diagnose_browser_warning(output, decision)
    request = request.model_copy(
        update={"allow_browser": request.hydrate and decision.allows_fallback}
    )
    validate_discovery_tokens(request)
    api_key = default_discovery_secrets().get_brave_api_key()
    if not api_key:
        raise WxcliError(ErrorCode.AUTHENTICATION_ERROR, "The Brave API key is not configured.")
    with httpx.Client(timeout=30.0) as client:
        http_evidence = (
            EvidenceService(PublicHttpProvider(client, default_cache())) if request.hydrate else None
        )
        browser_evidence = (
            ChromeEvidenceService(
                ChromeProvider(default_browser_profile(), cache=default_cache())
            )
            if request.allow_browser
            else None
        )
        service = DiscoveryService(
            BraveDiscoveryProvider(client, api_key),
            default_discovery_store(),
            http_evidence=http_evidence,
            browser_evidence=browser_evidence,
            browser_decision=decision,
        )
        result = service.search(request)
        output.success(
            _discovery_media_analysis_result(result) if analyze_media else result
        )


@discovery_app.command("hydrate")
def discovery_hydrate(
    context: typer.Context,
    input_path: str = typer.Option(..., "--input", help="Candidate Batch JSON file, or - for stdin."),
    priority_hydrate: int | None = typer.Option(None, "--priority-hydrate", min=0, max=20),
    max_hydrate: int | None = typer.Option(None, "--max-hydrate", min=0, max=20),
    require_account_match: bool = typer.Option(False, "--require-account-match"),
    require_published_date: bool = typer.Option(False, "--require-published-date"),
    browser: bool = typer.Option(
        False,
        "--browser",
        help="Explicitly allow serial Chrome fallback after HTTP verification pages.",
    ),
    browser_fallback: bool = typer.Option(
        False,
        "--browser-fallback",
        help="Allow one-shot serial Chrome fallback after HTTP verification pages.",
    ),
    no_browser: bool = typer.Option(
        False,
        "--no-browser",
        help="Prohibit Chrome even when durable fallback is enabled.",
    ),
    analyze_media: bool = typer.Option(
        False,
        "--analyze-media",
        help="Explicitly download eligible images and run local QR/OCR analysis.",
    ),
) -> None:
    """Validate and hydrate one agent-orchestrated Candidate Batch."""
    output = _output(context)
    batch = _candidate_batch_request(input_path)
    effective_priority = (
        batch.hydration.priority_count
        if priority_hydrate is None
        else priority_hydrate
    )
    effective_maximum = (
        batch.hydration.maximum_attempts if max_hydrate is None else max_hydrate
    )
    if effective_priority > effective_maximum:
        raise ValidationError("priority_hydrate must not exceed max_hydrate.")
    decision = resolve_browser_decision(
        default_browser_policy(),
        browser=browser,
        browser_fallback=browser_fallback,
        no_browser=no_browser,
    )
    _diagnose_browser_warning(output, decision)
    with httpx.Client(timeout=30.0) as client:
        http_evidence = EvidenceService(PublicHttpProvider(client, default_cache()))
        browser_evidence = (
            ChromeEvidenceService(
                ChromeProvider(default_browser_profile(), cache=default_cache())
            )
            if decision.allows_fallback
            else None
        )
        service = CandidateIngestionService(
            default_discovery_store(),
            http_evidence=http_evidence,
            browser_evidence=browser_evidence,
        )
        result = service.ingest(
            batch,
            priority_hydrate=priority_hydrate,
            max_hydrate=max_hydrate,
            require_account_match=require_account_match,
            require_published_date=require_published_date,
            allow_browser=decision.allows_fallback,
            browser_decision=decision,
        )
        output.success(
            _discovery_media_analysis_result(result) if analyze_media else result
        )


@discovery_auth_app.command("configure")
def discovery_auth_configure(
    context: typer.Context,
    provider: str = typer.Option("brave", "--provider"),
) -> None:
    """Interactively store one discovery credential in Windows Credential Manager."""
    output = _output(context)
    _require_brave(provider)
    if output.json_mode or not is_interactive():
        raise InputError("Discovery credential setup requires an interactive terminal.")
    api_key = typer.prompt("Brave API key", hide_input=True, confirmation_prompt=True, err=True)
    default_discovery_secrets().set_brave_api_key(api_key)
    output.success({"provider": "brave", "configured": True})


@discovery_auth_app.command("status")
def discovery_auth_status(
    context: typer.Context,
    provider: str = typer.Option("brave", "--provider"),
) -> None:
    """Report only whether the Brave credential exists."""
    output = _output(context)
    _require_brave(provider)
    output.success(
        {
            "provider": "brave",
            "configured": default_discovery_secrets().get_brave_api_key() is not None,
        }
    )


@discovery_cache_app.command("clear")
def discovery_cache_clear(context: typer.Context) -> None:
    """Clear discovery cache and history without touching credentials or ArticleCache."""
    output = _output(context)
    output.success({"cleared": default_discovery_store().clear()})


@cache_app.command("clear")
def clear_cache(context: typer.Context) -> None:
    """Delete only successful public article cache records."""
    output = context.find_root().obj
    if not isinstance(output, Output):
        raise WxcliError(ErrorCode.GENERAL_ERROR, "The command output is unavailable.")
    output.success({"cleared": default_cache().clear()})


@media_app.command("doctor")
def media_doctor(context: typer.Context) -> None:
    """Check packaged image/QR support and optional local Windows OCR."""
    output = _output(context)
    report = default_media_doctor().run()
    output.success(report)
    if report.overall == "fail":
        raise typer.Exit(ExitCode.GENERAL)


@media_cache_app.command("clear")
def media_cache_clear(context: typer.Context) -> None:
    """Delete only dedicated Media Cache records."""
    output = _output(context)
    output.success({"cleared": default_media_cache().clear()})


@browser_app.command("login")
def browser_login(context: typer.Context) -> None:
    """Open the dedicated visible Chrome profile for manual WeChat login."""
    output = context.find_root().obj
    if not isinstance(output, Output):
        raise WxcliError(ErrorCode.GENERAL_ERROR, "The command output is unavailable.")
    output.diagnostic("Chrome is opening with the WeChat OA-only profile.")
    ChromeProvider(default_browser_profile()).open_login()
    output.success({"opened": True, "session_validity": "not_verified"})


@browser_app.command("status")
def browser_status(context: typer.Context) -> None:
    """Report local profile facts without starting Chrome."""
    output = context.find_root().obj
    if not isinstance(output, Output):
        raise WxcliError(ErrorCode.GENERAL_ERROR, "The command output is unavailable.")
    status = default_browser_profile().status()
    output.success(
        {
            "profile_exists": status.profile_exists,
            "last_verified_at": status.last_verified_at,
            "legacy_last_verified_at": status.legacy_last_verified_at,
            "last_successful_read_at": status.last_successful_read_at,
            "session_validity": "not_verified",
        }
    )


@browser_app.command("clear")
def browser_clear(context: typer.Context) -> None:
    """Delete only WeChat OA's dedicated Chrome profile and local status record."""
    output = context.find_root().obj
    if not isinstance(output, Output):
        raise WxcliError(ErrorCode.GENERAL_ERROR, "The command output is unavailable.")
    default_browser_profile().clear()
    output.success({"cleared": True})


@browser_policy_app.command("set")
def browser_policy_set(
    context: typer.Context,
    policy: BrowserFallbackPolicy = typer.Argument(..., help="never or auto-fallback"),
) -> None:
    """Set durable local fallback authorization without touching browser session state."""
    output = _output(context)
    status = default_browser_policy().set(policy)
    output.success(
        {
            "policy": status.policy,
            "configured": status.configured,
            "valid": status.valid,
        }
    )


@browser_policy_app.command("status")
def browser_policy_status(context: typer.Context) -> None:
    """Report durable policy state without opening Chrome."""
    output = _output(context)
    status = default_browser_policy().status(strict=True)
    output.success(
        {
            "policy": status.policy,
            "configured": status.configured,
            "valid": status.valid,
        }
    )


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


def _output(context: typer.Context) -> Output:
    output = context.find_root().obj
    if not isinstance(output, Output):
        raise WxcliError(ErrorCode.GENERAL_ERROR, "The command output is unavailable.")
    return output


def _require_brave(provider: str) -> None:
    if provider.casefold() != "brave":
        raise ValidationError("Only the brave discovery provider is supported in WeChat OA 0.5.x.")


def _has_explicit_discovery_search_options(context: typer.Context) -> bool:
    option_names = (
        "query",
        "company",
        "account",
        "published_after",
        "published_before",
        "limit",
        "cursor",
        "checkpoint",
        "new_only",
        "hydrate",
        "priority_hydrate",
        "max_hydrate",
        "require_account_match",
        "require_published_date",
        "browser",
        "browser_fallback",
    )
    return any(
        (source := context.get_parameter_source(name)) is not None
        and source.name == "COMMANDLINE"
        for name in option_names
    )


def _discovery_request(
    *,
    query: str | None,
    input_path: str | None,
    companies: list[str],
    accounts: list[str],
    published_after: str | None,
    published_before: str | None,
    limit: int,
    cursor: str | None,
    checkpoint: str | None,
    new_only: bool,
    hydrate: bool,
    priority_hydrate: int,
    max_hydrate: int,
    require_account_match: bool,
    require_published_date: bool,
    allow_browser: bool,
) -> DiscoveryRequest:
    if input_path is not None:
        has_cli_search_options = any(
            (
                query is not None,
                bool(companies),
                bool(accounts),
                published_after is not None,
                published_before is not None,
                limit != 50,
                cursor is not None,
                checkpoint is not None,
                new_only,
                hydrate,
                priority_hydrate != 10,
                max_hydrate != 20,
                require_account_match,
                require_published_date,
                allow_browser,
            )
        )
        if has_cli_search_options:
            raise InputError("--input cannot be combined with query or search options.")
        try:
            raw = sys.stdin.read() if input_path == "-" else Path(input_path).read_text(encoding="utf-8")
        except OSError as error:
            raise InputError("The discovery input file could not be read.") from error
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise InputError("The discovery input is not valid JSON.") from error
    else:
        if query is None:
            raise InputError("Provide a discovery query or --input.")
        payload = {
            "schema_version": "1",
            "query": query,
            "companies": companies,
            "expected_accounts": [
                {"display_names": [display_name]} for display_name in accounts
            ],
            "published_after": published_after,
            "published_before": published_before,
            "limit": limit,
            "cursor": cursor,
            "checkpoint": checkpoint,
            "new_only": new_only,
            "hydrate": hydrate,
            "priority_hydrate": priority_hydrate,
            "max_hydrate": max_hydrate,
            "require_account_match": require_account_match,
            "require_published_date": require_published_date,
            "allow_browser": allow_browser,
        }
    try:
        return DiscoveryRequest.model_validate(payload)
    except PydanticValidationError as error:
        raise ValidationError("The discovery request does not match schema version 1.") from error


def _candidate_batch_request(input_path: str) -> CandidateBatchRequest:
    raw = _read_bounded_candidate_batch(input_path)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise InputError("The Candidate Batch input is not valid JSON.") from error
    try:
        return CandidateBatchRequest.model_validate(payload)
    except PydanticValidationError as error:
        raise ValidationError("The Candidate Batch does not match schema version 1.") from error


def _read_bounded_candidate_batch(input_path: str) -> str:
    raw: str | bytes
    try:
        if input_path == "-":
            stream = getattr(sys.stdin, "buffer", sys.stdin)
            raw = stream.read(MAX_CANDIDATE_BATCH_BYTES + 1)
        else:
            with Path(input_path).open("rb") as stream:
                raw = stream.read(MAX_CANDIDATE_BATCH_BYTES + 1)
    except OSError as error:
        raise InputError("The Candidate Batch input could not be read.") from error
    if isinstance(raw, str):
        encoded = raw.encode("utf-8")
        if len(encoded) > MAX_CANDIDATE_BATCH_BYTES:
            raise ValidationError("The Candidate Batch exceeds the 2 MiB limit.")
        return raw
    if len(raw) > MAX_CANDIDATE_BATCH_BYTES:
        raise ValidationError("The Candidate Batch exceeds the 2 MiB limit.")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InputError("The Candidate Batch input must be UTF-8.") from error


def _diagnose_browser_warning(output: Output, decision: BrowserDecision) -> None:
    if decision.warning:
        output.diagnostic(decision.warning)


def _attach_browser_warning(error: WxcliError, decision: BrowserDecision) -> None:
    if decision.warning:
        error.details.setdefault("warning", decision.warning)


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
