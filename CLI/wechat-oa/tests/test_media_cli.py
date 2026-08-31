from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from wxcli import cli
from wxcli.cli import app


class FakeMediaCache:
    def __init__(self) -> None:
        self.clear_calls = 0

    def clear(self) -> int:
        self.clear_calls += 1
        return 7


def test_media_cache_clear_uses_only_the_dedicated_cache(monkeypatch) -> None:
    cache = FakeMediaCache()
    monkeypatch.setattr(cli, "default_media_cache", lambda: cache)
    monkeypatch.setattr(
        cli,
        "default_cache",
        lambda: (_ for _ in ()).throw(AssertionError("Article Cache must stay untouched")),
    )
    monkeypatch.setattr(
        cli,
        "default_discovery_store",
        lambda: (_ for _ in ()).throw(AssertionError("Discovery state must stay untouched")),
    )

    result = CliRunner().invoke(app, ["--json", "media", "cache", "clear"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"] == {"cleared": 7}
    assert cache.clear_calls == 1


def test_default_media_cache_is_namespaced_under_runtime_root(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    cache = cli.default_media_cache()

    assert cache.directory == tmp_path / "wxcli" / "media-cache"


def test_media_help_exposes_only_cache_maintenance() -> None:
    result = CliRunner().invoke(app, ["media", "--help"])

    assert result.exit_code == 0
    assert "cache" in result.stdout
    assert "analyze" not in result.stdout
