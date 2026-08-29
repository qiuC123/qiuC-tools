import io
import json
from pathlib import Path

from typer.testing import CliRunner

import wxcli.cli as cli
from wxcli.discovery.models import DiscoveryRequest
from wxcli.cli import _candidate_batch_request, _discovery_request, app


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
        self.configured = configured

    def get_brave_api_key(self) -> str | None:
        return "secret" if self.configured else None


class FakeStore:
    def clear(self) -> int:
        return 3


def test_discovery_auth_status_and_cache_clear_are_offline(monkeypatch) -> None:
    monkeypatch.setattr(cli, "default_discovery_secrets", lambda: FakeSecrets())
    monkeypatch.setattr(cli, "default_discovery_store", lambda: FakeStore())
    runner = CliRunner()

    status = runner.invoke(app, ["--json", "discovery", "auth", "status", "--provider", "brave"])
    cleared = runner.invoke(app, ["--json", "discovery", "cache", "clear"])

    assert json.loads(status.stdout)["data"] == {"provider": "brave", "configured": True}
    assert json.loads(cleared.stdout)["data"] == {"cleared": 3}


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
    assert all(value in root.stdout for value in ("search", "hydrate", "auth", "cache"))
    assert all(value in search.stdout for value in ("--input", "--checkpoint", "--hydrate", "--browser"))
    assert all(value in hydrate.stdout for value in ("--input", "--max-hydrate", "--browser"))


def test_input_rejects_even_explicit_default_search_options(tmp_path: Path) -> None:
    path = tmp_path / "request.json"
    path.write_text('{"schema_version":"1","query":"x"}', encoding="utf-8")
    result = CliRunner().invoke(
        app, ["discovery", "search", "--input", str(path), "--limit", "50"]
    )
    assert result.exit_code != 0
    assert "--input cannot be combined" in str(result.exception)


def test_invalid_cursor_is_rejected_before_credentials_are_accessed(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "default_discovery_secrets",
        lambda: (_ for _ in ()).throw(AssertionError("credentials must not be accessed")),
    )

    result = CliRunner().invoke(
        app, ["discovery", "search", "campus", "--cursor", "damaged-token"]
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
    assert observed["options"] == {
        "priority_hydrate": 1,
        "max_hydrate": 1,
        "require_account_match": False,
        "require_published_date": False,
        "allow_browser": False,
    }


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
