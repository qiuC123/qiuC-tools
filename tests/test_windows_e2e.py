"""Windows command-boundary E2E tests with no real WeChat requests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

import wxcli.cli as cli
from wxcli.cache import ArticleCache
from wxcli.providers.official import OfficialAccountProvider

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="wxcli V1 supports Windows only")


def run_process(tmp_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the installed module in a clean per-test runtime directory."""
    environment = os.environ.copy()
    environment["LOCALAPPDATA"] = str(tmp_path / "runtime")
    environment["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "wxcli", *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        env=environment,
        check=False,
    )


def invoke_main(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *arguments: str,
) -> tuple[int, str, str]:
    """Invoke the real main/error boundary while allowing local dependency fakes."""
    monkeypatch.setattr(sys, "argv", ["wxcli", *arguments])
    exit_code = 0
    try:
        cli.main()
    except SystemExit as error:
        exit_code = int(error.code or 0)
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


def assert_one_json(stdout: str) -> dict[str, Any]:
    assert stdout.endswith("\n")
    assert len(stdout.splitlines()) == 1
    payload = json.loads(stdout)
    assert isinstance(payload, dict)
    return payload


def test_local_markdown_is_valid_utf8_json_from_a_real_process(tmp_path: Path) -> None:
    article_path = tmp_path / "本地文章.md"
    article_path.write_text("# 中文标题\n\n正文内容", encoding="utf-8")

    result = run_process(tmp_path, "--json", "article", "local", str(article_path))

    payload = assert_one_json(result.stdout)
    assert result.returncode == 0
    assert result.stderr == ""
    assert payload["ok"] is True
    assert payload["data"]["title"] == "中文标题"
    assert payload["data"]["provider"] == "local"


def test_invalid_cli_arguments_exit_two_with_one_json(tmp_path: Path) -> None:
    result = run_process(tmp_path, "--json", "article", "local")

    payload = assert_one_json(result.stdout)
    assert result.returncode == 2
    assert result.stderr == ""
    assert payload["ok"] is False
    assert payload["error"]["code"] == "INVALID_ARGUMENT"


@pytest.mark.parametrize(
    ("html", "exit_code", "error_code"),
    [
        ("<p>请完成安全验证</p>", 6, "VERIFICATION_REQUIRED"),
        ("<p>内容已被发布者删除</p>", 4, "NOT_FOUND"),
        ("<p>unexpected 200 response</p>", 8, "PARSING_ERROR"),
    ],
)
def test_http_200_exception_pages_keep_json_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    html: str,
    exit_code: int,
    error_code: str,
) -> None:
    real_client = httpx.Client

    def client_factory(*_: object, **__: object) -> httpx.Client:
        return real_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text=html))
        )

    monkeypatch.setattr(cli.httpx, "Client", client_factory)
    monkeypatch.setattr(cli, "default_cache", lambda: ArticleCache(tmp_path / "cache"))

    result = invoke_main(
        monkeypatch,
        capsys,
        "--json",
        "article",
        "get",
        "https://mp.weixin.qq.com/s/fixture",
    )

    actual_exit, stdout, stderr = result
    payload = assert_one_json(stdout)
    assert actual_exit == exit_code
    assert stderr == ""
    assert payload["error"]["code"] == error_code


def test_http_success_cache_and_no_cache_at_command_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    html = (
        '<h1 id="activity-name">缓存文章</h1><a id="js_name">作者</a>'
        '<div id="js_content"><p>正文</p></div><script>var ct=1760000000;</script>'
    )
    request_count = 0
    real_client = httpx.Client

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, text=html)

    def client_factory(*_: object, **__: object) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(respond))

    cache = ArticleCache(tmp_path / "cache")
    monkeypatch.setattr(cli.httpx, "Client", client_factory)
    monkeypatch.setattr(cli, "default_cache", lambda: cache)
    command = (
        "--json",
        "article",
        "get",
        "https://mp.weixin.qq.com/s/fixture",
    )

    first = invoke_main(monkeypatch, capsys, *command)
    second = invoke_main(monkeypatch, capsys, *command)
    uncached = invoke_main(monkeypatch, capsys, *command, "--no-cache")

    assert [result[0] for result in (first, second, uncached)] == [0, 0, 0]
    assert all(assert_one_json(result[1])["ok"] for result in (first, second, uncached))
    assert all(result[2] == "" for result in (first, second, uncached))
    assert request_count == 2


class StaticTokens:
    def with_token_retry(self, call: Callable[[str], Any]) -> Any:
        return call("fixture-credential")


def test_official_draft_and_published_list_get_commands_preserve_articles(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_client = httpx.Client

    def item(identifier_name: str, identifier: str, title: str) -> dict[str, Any]:
        return {
            identifier_name: identifier,
            "content": {
                "news_item": [
                    {"title": title, "content": "<p>头条</p>"},
                    {"title": f"{title}次条", "content": "<p>次条</p>"},
                ]
            },
            "update_time": 1_760_000_000,
        }

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/cgi-bin/draft/batchget":
            return httpx.Response(
                200,
                json={"item_count": 1, "item": [item("media_id", "media-fixture", "草稿")]},
            )
        if path == "/cgi-bin/draft/get":
            return httpx.Response(200, json=item("media_id", "unused", "草稿详情")["content"])
        if path == "/cgi-bin/freepublish/batchget":
            return httpx.Response(
                200,
                json={
                    "item_count": 1,
                    "item": [item("article_id", "article-fixture", "发布")],
                },
            )
        return httpx.Response(
            200,
            json={
                **item("article_id", "unused", "发布详情")["content"],
                "create_time": 1_760_000_000,
            },
        )

    def client_factory(*_: object, **__: object) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(respond))

    monkeypatch.setattr(cli.httpx, "Client", client_factory)
    monkeypatch.setattr(
        cli,
        "official_provider",
        lambda client: OfficialAccountProvider(client, StaticTokens()),
    )
    commands = [
        ("account", "draft", "list"),
        ("account", "draft", "get", "media-fixture"),
        ("account", "published", "list"),
        ("account", "published", "get", "article-fixture"),
    ]

    results = [invoke_main(monkeypatch, capsys, "--json", *command) for command in commands]

    assert all(result[0] == 0 and result[2] == "" for result in results)
    payloads = [assert_one_json(result[1]) for result in results]
    assert [article["index"] for article in payloads[0]["data"][0]["articles"]] == [0, 1]
    assert payloads[1]["data"]["media_id"] == "media-fixture"
    assert [article["index"] for article in payloads[2]["data"][0]["articles"]] == [0, 1]
    assert payloads[3]["data"]["article_id"] == "article-fixture"
