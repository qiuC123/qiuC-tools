"""Real installed MCP -> CLI -> GPU inference. No fake backend or silent skips."""
import asyncio
import hashlib
import os
from datetime import timedelta

from mcp import ClientSession
from mcp.client.stdio import stdio_client

from cli_anything.gpt_sovits.core.audio import inspect_wav
from cli_anything.gpt_sovits.core.config import Settings
from cli_anything.gpt_sovits.tests.test_full_e2e import _free_port, _make_system_tts_reference
from cli_anything.gpt_sovits.tests.test_mcp import connection


def test_real_mcp_inference(tmp_path):
    settings = Settings.discover(
        runtime=os.environ["GPT_SOVITS_E2E_RUNTIME"],
        api_url=f"http://127.0.0.1:{_free_port()}", state_dir=str(tmp_path / "mcp-state"),
    )
    settings.validate_backend()
    config_hash = hashlib.sha256(settings.tts_config.read_bytes()).hexdigest()
    reference = tmp_path / "reference.wav"
    prompt = _make_system_tts_reference(reference, "这是独立的接口参考音频")
    output = tmp_path / "mcp-output.wav"

    async def scenario():
        async with stdio_client(connection(settings)) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=660)) as session:
                await session.initialize()
                async def call(suffix, args=None):
                    result = await session.call_tool("gpt_sovits_" + suffix, args or {})
                    assert not result.isError, result
                    return result.structuredContent["data"]
                try:
                    started = await call("serve_start", {"timeout": 360})
                    assert started["ready"]
                    state = await call("serve_status")
                    assert state["identity_verified"]
                    assert (await call("doctor"))["ready"]
                    await call("serve_logs", {"lines": 5})
                    models = await call("model_list")
                    for kind in ("gpt", "sovits"):
                        assert models[kind]
                        plan = await call("model_use_" + kind, {"path": models[kind][0], "dry_run": True})
                        assert plan["dry_run"]
                    await call("reference_inspect", {"audio": str(reference)})
                    args = {"text": "你好，这是语音合成接口测试。", "text_lang": "zh", "ref_audio": str(reference),
                            "prompt_lang": "zh", "prompt_text": prompt, "output": str(output),
                            "text_split_method": "cut2", "seed": 42}
                    await call("synthesize", {**args, "dry_run": True})
                    assert not output.exists()
                    await call("synthesize", args)
                    report = inspect_wav(output, require_non_silent=True)
                    assert output.read_bytes()[:4] == b"RIFF" and report["duration_seconds"] > 0
                    original = output.read_bytes()
                    rejected = await session.call_tool("gpt_sovits_synthesize", args)
                    assert rejected.isError and rejected.structuredContent["error"]["code"] == "output_exists"
                    assert output.read_bytes() == original
                finally:
                    await call("serve_stop", {"timeout": 30})
                assert not (await call("serve_status"))["running"]
    try:
        asyncio.run(scenario())
    finally:
        assert hashlib.sha256(settings.tts_config.read_bytes()).hexdigest() == config_hash
    print(f"Verified MCP WAV: {output} ({output.stat().st_size} bytes)")
