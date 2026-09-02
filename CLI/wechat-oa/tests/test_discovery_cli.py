import io
import json
import sys
from pathlib import Path

import pytest
from click.utils import strip_ansi
from typer.testing import CliRunner

import wxcli.cli as cli
from wxcli.discovery.models import DiscoveryRequest
from wxcli.browser_policy import BrowserFallbackPolicy, BrowserPolicyStore
from wxcli.errors import VerificationRequiredError
from wxcli.cli import _candidate_batch_request, _discovery_request, app


@pytest.fixture(autouse=True)
def isolated_browser_policy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "default_browser_policy",
        lambda: BrowserPolicyStore(tmp_path / "default-browser-policy.json"),
    )


def request_kwargs() -> dict[str, object]:
    return {
        "query": "x",
        "input_path": None,
        "companies": [],
        "accounts": [],
        "published_after": None,
        "published_before": None,
        "limit": 50,
        "cursor": None,
        "checkpoint": None,
        "new_only": False,
        "hydrate": False,
        "priority_hydrate": 10,
        "max_hydrate": 20,
        "require_account_match": False,
        "require_published_date": False,
        "allow_browser": False,
    }


def test_cli_request_builds_repeatable_hints() -> None:
    values = request_kwargs()
    values.update(
        companies=["Acme"],
        accounts=["Acme Jobs"],
        published_after="2026-01-01",
        published_before="2026-12-31",
    )
    request = _discovery_request(**values)  # type: ignore[arg-type]

    assert request.companies == ["Acme"]
    assert request.expected_accounts[0].display_names == ["Acme Jobs"]


def test_json_file_and_stdin_input_are_supported_and_mutually_exclusive(tmp_path, monkeypatch) -> None:
    path = tmp_path / "request.json"
    path.write_text(json.dumps({"schema_version": "1", "query": "campus"}), encoding="utf-8")
    values = request_kwargs()
    values.update(query=None, input_path=str(path))
    assert _discovery_request(**values).query == "campus"  # type: ignore[arg-type]

    values["input_path"] = "-"
    monkeypatch.setattr("sys.stdin", io.StringIO('{"schema_version":"1","query":"stdin"}'))
    assert _discovery_request(**values).query == "stdin"  # type: ignore[arg-type]

    values["query"] = "conflict"
    try:
        _discovery_request(**values)  # type: ignore[arg-type]
    except Exception as error:
        assert getattr(error, "code", None) == "INVALID_ARGUMENT"
    else:
        raise AssertionError("Expected mutually exclusive input validation")


def test_json_input_rejects_malformed_unknown_and_unreadable_data(tmp_path) -> None:
    for name, content in (("bad.json", "{"), ("unknown.json", '{"query":"x","secret":"y"}')):
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        values = request_kwargs()
        values.update(query=None, input_path=str(path))
        try:
            _discovery_request(**values)  # type: ignore[arg-type]
        except Exception as error:
            assert getattr(error, "code", None) in {"INVALID_ARGUMENT", "VALIDATION_ERROR"}
        else:
            raise AssertionError("Expected invalid JSON input to fail")

    values = request_kwargs()
    values.update(query=None, input_path=str(tmp_path / "missing.json"))
    try:
        _discovery_request(**values)  # type: ignore[arg-type]
    except Exception as error:
        assert getattr(error, "code", None) == "INVALID_ARGUMENT"


class FakeSecrets:
    def __init__(self, configured: bool = True) -> None:
        self.values = (
            {"brave": "brave-secret", "exa": "exa-secret"}
            if configured
            else {}
        )

    def get_brave_api_key(self) -> str | None:
        return self.get_api_key("brave")

    def get_api_key(self, provider: str) -> str | None:
        return self.values.get(provider)

    def set_api_key(self, provider: str, value: str) -> None:
        self.values[provider] = value


class FakeStore:
    def clear(self) -> int:
        return 3


def test_discovery_auth_status_and_cache_clear_are_offline(monkeypatch) -> None:
    monkeypatch.setattr(cli, "default_discovery_secrets", lambda: FakeSecrets())
    monkeypatch.setattr(cli, "default_discovery_store", lambda: FakeStore())
    runner = CliRunner()

    status = runner.invoke(app, ["--json", "discovery", "auth", "status", "--provider", "brave"])
    exa_status = runner.invoke(
        app, ["--json", "discovery", "auth", "status", "--provider", "exa"]
    )
    cleared = runner.invoke(app, ["--json", "discovery", "cache", "clear"])

    assert json.loads(status.stdout)["data"] == {"provider": "brave", "configured": True}
    assert json.loads(exa_status.stdout)["data"] == {
        "provider": "exa",
        "configured": True,
    }
    assert json.loads(cleared.stdout)["data"] == {"cleared": 3}


def test_discovery_auth_configures_exa_without_exposing_the_key(monkeypatch) -> None:
    secrets = FakeSecrets(configured=False)
    monkeypatch.setattr(cli, "default_discovery_secrets", lambda: secrets)
    monkeypatch.setattr(cli, "is_interactive", lambda: True)
    monkeypatch.setattr(cli.typer, "prompt", lambda *args, **kwargs: "test-exa-secret")

    result = CliRunner().invoke(
        app,
        ["discovery", "auth", "configure", "--provider", "exa"],
    )

    assert result.exit_code == 0
    assert secrets.get_api_key("exa") == "test-exa-secret"
    assert "test-exa-secret" not in result.stdout
    assert "test-exa-secret" not in (result.stderr or "")


def test_discovery_rejects_unknown_provider_before_credentials(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "default_discovery_secrets",
        lambda: (_ for _ in ()).throw(
            AssertionError("credentials must not be accessed")
        ),
    )

    result = CliRunner().invoke(
        app,
        ["discovery", "search", "campus", "--provider", "unknown"],
    )

    assert result.exit_code != 0
    assert getattr(result.exception, "code", None) == "VALIDATION_ERROR"


def test_discovery_search_emits_one_json_document_without_constructing_chrome(monkeypatch) -> None:
    observed: list[DiscoveryRequest] = []

    class FakeDiscoveryService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def search(self, request: DiscoveryRequest) -> dict[str, object]:
            observed.append(request)
            return {"schema_version": "1", "candidates": []}

    monkeypatch.setattr(cli, "default_discovery_secrets", lambda: FakeSecrets())
    monkeypatch.setattr(cli, "default_discovery_store", lambda: FakeStore())
    monkeypatch.setattr(cli, "BraveDiscoveryProvider", lambda *args: object())
    monkeypatch.setattr(cli, "DiscoveryService", FakeDiscoveryService)
    monkeypatch.setattr(
        cli,
        "_discovery_media_analysis_result",
        lambda result: (_ for _ in ()).throw(
            AssertionError("Media Analysis must remain disabled")
        ),
    )
    monkeypatch.setattr(
        cli,
        "ChromeProvider",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Chrome must stay closed")),
    )

    result = CliRunner().invoke(
        app,
        ["--json", "discovery", "search", "campus", "--company", "Acme", "--account", "Jobs"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["schema_version"] == "1"
    assert observed[0].companies == ["Acme"]
    assert observed[0].allow_browser is False


def test_discovery_search_selects_exa_for_json_request(monkeypatch, tmp_path: Path) -> None:
    request_path = tmp_path / "request.json"
    request_path.write_text(
        '{"schema_version":"1","query":"campus"}',
        encoding="utf-8",
    )
    observed: dict[str, object] = {}

    class FakeDiscoveryService:
        def __init__(self, provider: object, *args: object, **kwargs: object) -> None:
            observed["provider"] = provider

        def search(self, request: DiscoveryRequest) -> dict[str, object]:
            observed["request"] = request
            return {"schema_version": "1", "search_provider": "exa", "candidates": []}

    monkeypatch.setattr(cli, "default_discovery_secrets", lambda: FakeSecrets())
    monkeypatch.setattr(cli, "default_discovery_store", lambda: FakeStore())
    monkeypatch.setattr(
        cli,
        "ExaDiscoveryProvider",
        lambda client, api_key: ("exa-provider", api_key),
    )
    monkeypatch.setattr(cli, "DiscoveryService", FakeDiscoveryService)

    result = CliRunner().invoke(
        app,
        [
            "--json",
            "discovery",
            "search",
            "--input",
            str(request_path),
            "--provider",
            "exa",
        ],
    )

    assert result.exit_code == 0
    assert observed["provider"] == ("exa-provider", "exa-secret")
    assert isinstance(observed["request"], DiscoveryRequest)
    assert json.loads(result.stdout)["data"]["search_provider"] == "exa"


def test_recruitment_exa_command_contract_uses_keyring_and_keeps_chrome_closed(
    monkeypatch,
) -> None:
    observed: dict[str, object] = {}

    class KeyringOnlySecrets(FakeSecrets):
        def get_api_key(self, provider: str) -> str | None:
            observed["credential_provider"] = provider
            return "keyring-exa-secret"

    class FakeDiscoveryService:
        def __init__(self, provider: object, *args: object, **kwargs: object) -> None:
            observed["provider"] = provider

        def search(self, request: DiscoveryRequest) -> dict[str, object]:
            observed["request"] = request
            return {
                "schema_version": "1",
                "search_provider": "exa",
                "summary": {"partial": False},
                "candidates": [],
            }

    monkeypatch.setenv("EXA_API_KEY", "environment-secret-must-be-ignored")
    monkeypatch.setattr(cli, "default_discovery_secrets", lambda: KeyringOnlySecrets())
    monkeypatch.setattr(cli, "default_discovery_store", lambda: FakeStore())
    monkeypatch.setattr(cli, "PublicHttpProvider", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "EvidenceService", lambda provider: object())
    monkeypatch.setattr(
        cli,
        "ExaDiscoveryProvider",
        lambda client, api_key: ("exa-provider", api_key),
    )
    monkeypatch.setattr(cli, "DiscoveryService", FakeDiscoveryService)
    monkeypatch.setattr(
        cli,
        "ChromeProvider",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Chrome must stay closed")
        ),
    )

    result = CliRunner().invoke(
        app,
        [
            "--json",
            "discovery",
            "search",
            "campus",
            "--company",
            "Acme",
            "--account",
            "Jobs",
            "--provider",
            "exa",
            "--hydrate",
            "--no-browser",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout.count("\n") == 1
    assert "environment-secret-must-be-ignored" not in result.stdout
    assert "keyring-exa-secret" not in result.stdout
    assert observed["credential_provider"] == "exa"
    assert observed["provider"] == ("exa-provider", "keyring-exa-secret")
    request = observed["request"]
    assert isinstance(request, DiscoveryRequest)
    assert request.companies == ["Acme"]
    assert request.expected_accounts[0].display_names == ["Jobs"]
    assert request.hydrate is True
    assert request.allow_browser is False


def test_json_missing_exa_key_has_one_stable_error_envelope(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli,
        "default_discovery_secrets",
        lambda: FakeSecrets(configured=False),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["wechat-oa", "--json", "discovery", "search", "campus", "--provider", "exa"],
    )

    with pytest.raises(SystemExit) as raised:
        cli.main()

    captured = capsys.readouterr()
    assert raised.value.code == 6
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "ok": False,
        "error": {
            "code": "AUTHENTICATION_ERROR",
            "message": "The Exa API key is not configured.",
            "details": {"provider": "exa", "reason": "not_configured"},
        },
    }


def test_discovery_search_reports_missing_selected_provider_key(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "default_discovery_secrets",
        lambda: FakeSecrets(configured=False),
    )

    result = CliRunner().invoke(
        app,
        ["discovery", "search", "campus", "--provider", "exa"],
    )

    assert result.exit_code != 0
    assert getattr(result.exception, "code", None) == "AUTHENTICATION_ERROR"
    assert result.exception.details == {
        "provider": "exa",
        "reason": "not_configured",
    }
    assert "Exa API key is not configured" in str(result.exception)


def test_article_evidence_command_uses_fresh_evidence_path(monkeypatch) -> None:
    class FakeEvidenceService:
        def __init__(self, provider: object) -> None:
            pass

        def get(self, url: str) -> dict[str, str]:
            return {"url": url, "kind": "evidence"}

    monkeypatch.setattr(cli, "PublicHttpProvider", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "EvidenceService", FakeEvidenceService)
    result = CliRunner().invoke(
        app, ["--json", "article", "evidence", "https://mp.weixin.qq.com/s/TOKEN"]
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["kind"] == "evidence"


def test_discovery_help_exposes_stable_commands() -> None:
    root = CliRunner().invoke(app, ["discovery", "--help"])
    search = CliRunner().invoke(app, ["discovery", "search", "--help"])
    hydrate = CliRunner().invoke(app, ["discovery", "hydrate", "--help"])
    root_help = strip_ansi(root.stdout)
    search_help = strip_ansi(search.stdout)
    hydrate_help = strip_ansi(hydrate.stdout)

    assert all(value in root_help for value in ("search", "hydrate", "auth", "cache"))
    assert all(
        value in search_help
        for value in ("--input", "--provider", "--checkpoint", "--hydrate", "--analyze-media", "--browser", "--browser-fallback", "--no-browser")
    )
    assert all(
        value in hydrate_help
        for value in ("--input", "--max-hydrate", "--analyze-media", "--browser", "--browser-fallback", "--no-browser")
    )


def test_input_rejects_even_explicit_default_search_options(tmp_path: Path) -> None:
    path = tmp_path / "request.json"
    path.write_text('{"schema_version":"1","query":"x"}', encoding="utf-8")
    result = CliRunner().invoke(
        app, ["discovery", "search", "--input", str(path), "--limit", "50"]
    )
    assert result.exit_code != 0
    assert "--input cannot be combined" in str(result.exception)


@pytest.mark.parametrize("provider", ["brave", "exa"])
def test_invalid_cursor_is_rejected_before_credentials_are_accessed(
    monkeypatch, provider: str
) -> None:
    monkeypatch.setattr(
        cli,
        "default_discovery_secrets",
        lambda: (_ for _ in ()).throw(AssertionError("credentials must not be accessed")),
    )

    result = CliRunner().invoke(
        app,
        [
            "discovery",
            "search",
            "campus",
            "--provider",
            provider,
            "--cursor",
            "damaged-token",
        ],
    )

    assert result.exit_code != 0
    assert getattr(result.exception, "code", None) == "VALIDATION_ERROR"


def candidate_batch_json() -> str:
    return json.dumps(
        {
            "schema_version": "1",
            "discovery_request": {"query": "campus"},
            "source": {"orchestrator": "codex", "providers": ["exa"]},
            "candidates": [
                {
                    "url": "https://mp.weixin.qq.com/s/T1",
                    "search_provenance": {"provider": "exa", "rank": 1},
                }
            ],
        }
    )


def test_candidate_batch_reader_supports_file_stdin_and_bounded_utf8(tmp_path, monkeypatch) -> None:
    path = tmp_path / "batch.json"
    path.write_text(candidate_batch_json(), encoding="utf-8")
    assert _candidate_batch_request(str(path)).source.orchestrator == "codex"

    monkeypatch.setattr("sys.stdin", io.StringIO(candidate_batch_json()))
    assert _candidate_batch_request("-").candidates[0].search_provenance.provider == "exa"

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    with __import__("pytest").raises(Exception) as too_large:
        _candidate_batch_request(str(oversized))
    assert getattr(too_large.value, "code", None) == "VALIDATION_ERROR"

    invalid_utf8 = tmp_path / "invalid.json"
    invalid_utf8.write_bytes(b"\xff")
    with __import__("pytest").raises(Exception) as invalid:
        _candidate_batch_request(str(invalid_utf8))
    assert getattr(invalid.value, "code", None) == "INVALID_ARGUMENT"


def test_candidate_batch_rejects_secrets_and_cannot_authorize_browser(tmp_path) -> None:
    payload = json.loads(candidate_batch_json())
    payload["api_key"] = "secret"
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps(payload), encoding="utf-8")
    with __import__("pytest").raises(Exception) as rejected:
        _candidate_batch_request(str(secret))
    assert getattr(rejected.value, "code", None) == "VALIDATION_ERROR"

    payload = json.loads(candidate_batch_json())
    payload["hydration"] = {
        "priority_count": 1,
        "maximum_attempts": 1,
        "allow_browser": True,
    }
    delegated = tmp_path / "delegated.json"
    delegated.write_text(json.dumps(payload), encoding="utf-8")
    with __import__("pytest").raises(Exception):
        _candidate_batch_request(str(delegated))

    payload = json.loads(candidate_batch_json())
    payload["analyze_media"] = True
    delegated_media = tmp_path / "delegated-media.json"
    delegated_media.write_text(json.dumps(payload), encoding="utf-8")
    with __import__("pytest").raises(Exception):
        _candidate_batch_request(str(delegated_media))


def test_discovery_hydrate_is_offline_from_search_and_does_not_open_chrome(tmp_path, monkeypatch) -> None:
    path = tmp_path / "batch.json"
    path.write_text(candidate_batch_json(), encoding="utf-8")
    observed: dict[str, object] = {}

    class FakeIngestionService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            observed["browser_evidence"] = kwargs.get("browser_evidence")

        def ingest(self, batch, **kwargs):
            observed["batch"] = batch
            observed["options"] = kwargs
            return {"schema_version": "1", "discovery_mode": "agent_orchestrated"}

    monkeypatch.setattr(
        cli,
        "default_discovery_secrets",
        lambda: (_ for _ in ()).throw(AssertionError("Brave credentials must stay untouched")),
    )
    monkeypatch.setattr(cli, "default_discovery_store", lambda: object())
    monkeypatch.setattr(cli, "PublicHttpProvider", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "EvidenceService", lambda provider: object())
    monkeypatch.setattr(cli, "CandidateIngestionService", FakeIngestionService)
    monkeypatch.setattr(
        cli,
        "_discovery_media_analysis_result",
        lambda result: (_ for _ in ()).throw(
            AssertionError("Media Analysis must remain disabled")
        ),
    )
    monkeypatch.setattr(
        cli,
        "ChromeProvider",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Chrome must stay closed")),
    )

    result = CliRunner().invoke(
        app,
        [
            "--json",
            "discovery",
            "hydrate",
            "--input",
            str(path),
            "--priority-hydrate",
            "1",
            "--max-hydrate",
            "1",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["discovery_mode"] == "agent_orchestrated"
    assert observed["browser_evidence"] is None
    options = observed["options"]
    assert isinstance(options, dict)
    decision = options.pop("browser_decision")
    assert getattr(decision, "mode", None) == "never"
    assert options == {
        "priority_hydrate": 1,
        "max_hydrate": 1,
        "require_account_match": False,
        "require_published_date": False,
        "allow_browser": False,
    }


def test_discovery_search_media_requires_hydration_before_credentials(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "default_discovery_secrets",
        lambda: (_ for _ in ()).throw(
            AssertionError("credentials must not be accessed")
        ),
    )

    result = CliRunner().invoke(
        app,
        ["discovery", "search", "campus", "--analyze-media"],
    )

    assert result.exit_code != 0
    assert "--analyze-media requires --hydrate" in str(result.exception)


def test_discovery_search_explicit_media_wraps_the_hydrated_result(monkeypatch) -> None:
    observed: dict[str, object] = {}

    class FakeDiscoveryService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def search(self, request: DiscoveryRequest) -> dict[str, object]:
            observed["request"] = request
            return {"schema_version": "1", "candidates": []}

    def wrap(result):
        observed["core"] = result
        return {"schema_version": "2", "discovery_result": result, "media": []}

    monkeypatch.setattr(cli, "default_discovery_secrets", lambda: FakeSecrets())
    monkeypatch.setattr(cli, "default_discovery_store", lambda: object())
    monkeypatch.setattr(cli, "BraveDiscoveryProvider", lambda *args: object())
    monkeypatch.setattr(cli, "PublicHttpProvider", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "EvidenceService", lambda provider: object())
    monkeypatch.setattr(cli, "DiscoveryService", FakeDiscoveryService)
    monkeypatch.setattr(cli, "_discovery_media_analysis_result", wrap)

    result = CliRunner().invoke(
        app,
        [
            "--json",
            "discovery",
            "search",
            "campus",
            "--hydrate",
            "--analyze-media",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["schema_version"] == "2"
    assert isinstance(observed["request"], DiscoveryRequest)
    assert observed["request"].hydrate is True
    assert observed["core"] == {"schema_version": "1", "candidates": []}


def test_discovery_hydrate_explicit_media_wraps_ingestion_result(tmp_path, monkeypatch) -> None:
    path = tmp_path / "batch.json"
    path.write_text(candidate_batch_json(), encoding="utf-8")
    observed: dict[str, object] = {}

    class FakeIngestionService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def ingest(self, batch, **kwargs):
            return {"schema_version": "1", "candidates": []}

    def wrap(result):
        observed["core"] = result
        return {"schema_version": "2", "discovery_result": result, "media": []}

    monkeypatch.setattr(cli, "default_discovery_store", lambda: object())
    monkeypatch.setattr(cli, "PublicHttpProvider", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "EvidenceService", lambda provider: object())
    monkeypatch.setattr(cli, "CandidateIngestionService", FakeIngestionService)
    monkeypatch.setattr(cli, "_discovery_media_analysis_result", wrap)

    result = CliRunner().invoke(
        app,
        [
            "--json",
            "discovery",
            "hydrate",
            "--input",
            str(path),
            "--analyze-media",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["schema_version"] == "2"
    assert observed["core"] == {"schema_version": "1", "candidates": []}


def test_discovery_hydrate_browser_requires_explicit_cli_flag(tmp_path, monkeypatch) -> None:
    path = tmp_path / "batch.json"
    path.write_text(candidate_batch_json(), encoding="utf-8")
    observed: dict[str, object] = {}

    class FakeIngestionService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            observed["browser_evidence"] = kwargs.get("browser_evidence")

        def ingest(self, batch, **kwargs):
            observed["allow_browser"] = kwargs["allow_browser"]
            return {"schema_version": "1"}

    monkeypatch.setattr(cli, "default_discovery_store", lambda: object())
    monkeypatch.setattr(cli, "PublicHttpProvider", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "ChromeProvider", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "EvidenceService", lambda provider: provider)
    monkeypatch.setattr(cli, "CandidateIngestionService", FakeIngestionService)

    result = CliRunner().invoke(
        app,
        ["--json", "discovery", "hydrate", "--input", str(path), "--browser"],
    )

    assert result.exit_code == 0
    assert observed["browser_evidence"] is not None
    assert observed["allow_browser"] is True


def test_browser_policy_commands_are_local_and_do_not_open_chrome(tmp_path, monkeypatch) -> None:
    store = BrowserPolicyStore(tmp_path / "browser-policy.json")
    monkeypatch.setattr(cli, "default_browser_policy", lambda: store)
    monkeypatch.setattr(
        cli,
        "ChromeProvider",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Chrome must stay closed")),
    )
    runner = CliRunner()

    configured = runner.invoke(
        app,
        ["--json", "browser", "policy", "set", "auto-fallback"],
    )
    status = runner.invoke(app, ["--json", "browser", "policy", "status"])

    assert configured.exit_code == 0
    assert json.loads(configured.stdout)["data"]["policy"] == "auto-fallback"
    assert json.loads(status.stdout)["data"] == {
        "policy": "auto-fallback",
        "configured": True,
        "valid": True,
    }


def test_article_fallback_runs_http_first_and_no_browser_overrides_policy(tmp_path, monkeypatch) -> None:
    store = BrowserPolicyStore(tmp_path / "browser-policy.json")
    store.set(BrowserFallbackPolicy.AUTO_FALLBACK)
    monkeypatch.setattr(cli, "default_browser_policy", lambda: store)
    calls: list[str] = []

    class FakeHttp:
        def __init__(self, *args, **kwargs) -> None: pass
        def get(self, url: str, *, no_cache: bool = False):
            calls.append("http")
            raise VerificationRequiredError()

    class FakeChrome:
        def __init__(self, *args, **kwargs) -> None: pass
        def get(self, url: str, *, no_cache: bool = False):
            calls.append("chrome")
            return {"provider": "chrome", "url": url}

    monkeypatch.setattr(cli, "PublicHttpProvider", FakeHttp)
    monkeypatch.setattr(cli, "ChromeProvider", FakeChrome)
    runner = CliRunner()
    allowed = runner.invoke(
        app,
        ["--json", "article", "get", "https://mp.weixin.qq.com/s/T1"],
    )
    assert allowed.exit_code == 0
    assert calls == ["http", "chrome"]

    calls.clear()
    prohibited = runner.invoke(
        app,
        ["article", "get", "https://mp.weixin.qq.com/s/T1", "--no-browser"],
    )
    assert prohibited.exit_code != 0
    assert getattr(prohibited.exception, "code", None) == "VERIFICATION_REQUIRED"
    assert calls == ["http"]


def test_direct_request_json_can_grant_once_but_no_browser_wins(tmp_path, monkeypatch) -> None:
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "query": "campus",
                "hydrate": True,
                "allow_browser": True,
            }
        ),
        encoding="utf-8",
    )
    observed: dict[str, object] = {}

    class FakeDiscoveryService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            observed["browser_evidence"] = kwargs.get("browser_evidence")
            observed["decision"] = kwargs.get("browser_decision")
        def search(self, request: DiscoveryRequest) -> dict[str, object]:
            observed["request"] = request
            return {"schema_version": "1", "candidates": []}

    monkeypatch.setattr(cli, "default_browser_policy", lambda: BrowserPolicyStore(tmp_path / "missing.json"))
    monkeypatch.setattr(cli, "default_discovery_secrets", lambda: FakeSecrets())
    monkeypatch.setattr(cli, "default_discovery_store", lambda: object())
    monkeypatch.setattr(cli, "BraveDiscoveryProvider", lambda *args: object())
    monkeypatch.setattr(cli, "PublicHttpProvider", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "EvidenceService", lambda provider: object())
    monkeypatch.setattr(cli, "DiscoveryService", FakeDiscoveryService)
    monkeypatch.setattr(
        cli,
        "ChromeProvider",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Chrome must stay closed")),
    )

    result = CliRunner().invoke(
        app,
        ["--json", "discovery", "search", "--input", str(request_path), "--no-browser"],
    )

    assert result.exit_code == 0
    assert observed["browser_evidence"] is None
    assert getattr(observed["decision"], "mode", None) == "never"
    assert getattr(observed["request"], "allow_browser", None) is False


def test_conflicting_browser_controls_are_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "default_browser_policy", lambda: BrowserPolicyStore(tmp_path / "policy.json"))
    result = CliRunner().invoke(
        app,
        [
            "article",
            "get",
            "https://mp.weixin.qq.com/s/T1",
            "--browser-fallback",
            "--no-browser",
        ],
    )
    assert result.exit_code != 0
    assert getattr(result.exception, "code", None) == "INVALID_ARGUMENT"
