from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from datetime import timedelta
from unittest.mock import AsyncMock

import jsonschema
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolRequest, CallToolRequestParams

from cli_anything.gpt_sovits import mcp_server as adapter
from cli_anything.gpt_sovits.core.config import Settings
from cli_anything.gpt_sovits.core.errors import CLIError
from cli_anything.gpt_sovits.core.output import envelope
from cli_anything.gpt_sovits.tests.test_core import make_wav


def _resolve_cli():
    path = shutil.which("gpt-sovits-mcp")
    if path:
        return [path]
    if os.environ.get("CLI_ANYTHING_FORCE_INSTALLED") == "1":
        pytest.fail("Install .[mcp,test] first: gpt-sovits-mcp is missing")
    return [sys.executable, "-m", "cli_anything.gpt_sovits.mcp_server"]


def connection(settings):
    command = _resolve_cli()
    args = command[1:]
    for key in ("checkout", "runtime", "api_url", "tts_config", "state_dir"):
        args.append(f"--{key.replace('_', '-')}={getattr(settings, key)}")
    return StdioServerParameters(command=command[0], args=args,
                                 env={**os.environ, "PYTHONUTF8": "1"})


def test_tool_inventory():
    assert len(adapter.TOOL_PATHS) == 10
    assert all("training" not in name and "execute" not in name for name in adapter.TOOL_PATHS)
    for name in adapter.TOOL_PATHS:
        jsonschema.Draft202012Validator.check_schema(adapter.input_schema(name))
        json.dumps(adapter.input_schema(name))


def test_schema_matches_cli_defaults():
    spec = adapter.input_schema("gpt_sovits_synthesize")
    assert set(spec["required"]) == {"text_lang", "ref_audio", "prompt_lang", "output"}
    props = spec["properties"]
    assert props["top_k"] == {"type": "integer", "minimum": 1, "default": 15}
    assert props["temperature"]["exclusiveMinimum"] == 0
    assert props["text_split_method"]["default"] == "cut5"
    assert props["parallel_infer"]["default"] is True
    assert "dry_run" in props and "command_json" not in props


@pytest.mark.parametrize("change", [
    {"temperature": 0}, {"top_k": 0}, {"top_k": 1.5}, {"top_p": 1.1},
    {"speed_factor": 0}, {"parallel_infer": "false"}, {"shell": "whoami"},
    {"output": None}, {"timeout": -1},
])
def test_invalid_schema_input(change):
    args = {"text_lang": "zh", "ref_audio": "ref.wav", "prompt_lang": "zh", "output": "out.wav", **change}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(args, adapter.input_schema("gpt_sovits_synthesize"))


def test_argv_keeps_values_literal_and_boolean_negation():
    args = adapter.command_args("gpt_sovits_synthesize", {
        "text": '--help; "中文"\nnext', "parallel_infer": False,
        "split_bucket": False, "dry_run": True, "overwrite": False,
    })
    assert '--text=--help; "中文"\nnext' in args
    assert "--no-parallel-infer" in args and "--no-split-bucket" in args
    assert "--dry-run" in args and "--overwrite" not in args
    assert adapter.command_args("gpt_sovits_model_use_gpt", {"path": "--help"}) == ["model", "use-gpt", "--", "--help"]


@pytest.mark.parametrize("body,code,expected", [
    (b"not JSON", 1, "invalid_cli_response"),
    (b"[]", 0, "invalid_cli_response"),
    (b'{"ok":true}', 1, "cli_failed"),
])
def test_invalid_child_response(monkeypatch, tmp_path, body, code, expected):
    child = AsyncMock()
    child.returncode = code
    child.communicate.return_value = (body, b"diagnostic")
    spawn = AsyncMock(return_value=child)
    monkeypatch.setattr(adapter.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(CLIError) as error:
        asyncio.run(adapter.run_cli(Settings.discover(checkout=str(tmp_path)), "gpt_sovits_doctor", {}))
    assert error.value.code == expected
    assert spawn.call_args.kwargs["stdin"] == asyncio.subprocess.DEVNULL


def test_timeout_kills_child(monkeypatch, tmp_path):
    class Child:
        returncode = None
        killed = False
        async def communicate(self):
            if not self.killed:
                await asyncio.sleep(30)
            return b"", b""
        def kill(self):
            self.killed = True
            self.returncode = -1
    child = Child()
    monkeypatch.setattr(adapter.asyncio, "create_subprocess_exec", AsyncMock(return_value=child))
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(adapter.run_cli(Settings.discover(checkout=str(tmp_path)), "gpt_sovits_doctor", {}, deadline=0.01))
    assert child.killed


def test_cancellation_reaps_child(monkeypatch, tmp_path):
    class Child:
        returncode = None
        killed = False
        async def communicate(self):
            if not self.killed:
                raise asyncio.CancelledError()
            return b"", b""
        def kill(self):
            self.killed = True
            self.returncode = -1
    child = Child()
    monkeypatch.setattr(adapter.asyncio, "create_subprocess_exec", AsyncMock(return_value=child))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(adapter.run_cli(Settings.discover(checkout=str(tmp_path)), "gpt_sovits_doctor", {}))
    assert child.killed


def test_serialized_calls_and_error_flag(monkeypatch, tmp_path):
    active = 0
    maximum = 0
    async def fake_run(*args):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return envelope("doctor", error={"code": "example", "message": "test"})
    monkeypatch.setattr(adapter, "run_cli", fake_run)
    async def scenario():
        server = adapter.create_server(Settings.discover(checkout=str(tmp_path)))
        handler = server.request_handlers[CallToolRequest]
        request = CallToolRequest(method="tools/call", params=CallToolRequestParams(name="gpt_sovits_doctor", arguments={}))
        results = await asyncio.gather(handler(request), handler(request))
        assert all(r.root.isError for r in results)
    asyncio.run(scenario())
    assert maximum == 1


def test_installed_help_and_configuration_error(tmp_path):
    result = subprocess.run(_resolve_cli() + ["--help"], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0 and "stdio" in result.stdout
    result = subprocess.run(_resolve_cli() + ["--checkout", str(tmp_path), "--api-url", "http://example.com:9880"],
                            capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"}, timeout=30)
    assert result.returncode != 0 and result.stdout == "" and result.stderr


def test_installed_stdio_protocol_and_dry_run(tmp_path):
    reference = make_wav(tmp_path / "参考 音频.wav")
    output = tmp_path / "输出.wav"
    settings = Settings.discover(checkout=str(tmp_path), state_dir=str(tmp_path / "state"))
    async def scenario():
        async with stdio_client(connection(settings)) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "gpt-sovits"
                listed = await session.list_tools()
                assert {t.name for t in listed.tools} == set(adapter.TOOL_PATHS)
                invalid = await session.call_tool("gpt_sovits_synthesize", {})
                assert invalid.isError
                unknown = await session.call_tool("not_a_tool", {})
                assert unknown.isError
                inspected = await session.call_tool("gpt_sovits_reference_inspect", {"audio": str(reference)})
                assert not inspected.isError and inspected.structuredContent["ok"]
                args = {"text": "中文测试", "text_lang": "zh", "prompt_lang": "zh", "ref_audio": str(reference),
                        "output": str(output), "dry_run": True, "text_split_method": "cut2",
                        "parallel_infer": False, "seed": 42}
                result = await session.call_tool("gpt_sovits_synthesize", args)
                assert not result.isError, result
                payload = result.structuredContent["data"]["parameters"]
                assert payload["text"] == "中文测试" and payload["text_split_method"] == "cut2"
                assert payload["parallel_infer"] is False and payload["seed"] == 42
                assert json.loads(result.content[0].text) == result.structuredContent
                assert not output.exists()
                failed = await session.call_tool("gpt_sovits_reference_inspect", {"audio": str(tmp_path / "missing.wav")})
                assert failed.isError and failed.structuredContent["ok"] is False
    asyncio.run(scenario())
