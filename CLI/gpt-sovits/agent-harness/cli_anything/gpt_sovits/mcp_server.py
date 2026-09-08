"""Local stdio MCP adapter. Inference remains in the installed CLI/backend."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

import click

from cli_anything.gpt_sovits import __version__
from cli_anything.gpt_sovits.core.config import Settings
from cli_anything.gpt_sovits.core.errors import CLIError
from cli_anything.gpt_sovits.core.output import envelope
from cli_anything.gpt_sovits.gpt_sovits_cli import cli


# An explicit inventory, never an arbitrary command/shell execution tool.
TOOL_PATHS = {
    "gpt_sovits_doctor": ("doctor",),
    "gpt_sovits_serve_start": ("serve", "start"),
    "gpt_sovits_serve_status": ("serve", "status"),
    "gpt_sovits_serve_stop": ("serve", "stop"),
    "gpt_sovits_serve_logs": ("serve", "logs"),
    "gpt_sovits_model_list": ("model", "list"),
    "gpt_sovits_model_use_gpt": ("model", "use-gpt"),
    "gpt_sovits_model_use_sovits": ("model", "use-sovits"),
    "gpt_sovits_reference_inspect": ("reference", "inspect"),
    "gpt_sovits_synthesize": ("synthesize",),
}
READ_ONLY = {"doctor", "serve.logs", "model.list", "reference.inspect"}


def command_for(name: str) -> click.Command:
    command = cli
    for part in TOOL_PATHS[name]:
        command = command.commands[part]
    return command


def parameters(name: str) -> dict[str, click.Parameter]:
    return {
        "dry_run" if p.name == "command_dry_run" else p.name: p
        for p in command_for(name).params if p.name != "command_json"
    }


def input_schema(name: str) -> dict:
    """Derive types, defaults and bounds from the existing Click contract."""
    properties, required = {}, []
    for key, param in parameters(name).items():
        kind = param.type
        if isinstance(kind, click.types.BoolParamType):
            spec = {"type": "boolean"}
        elif isinstance(kind, click.types.IntParamType):
            spec = {"type": "integer"}
        elif isinstance(kind, click.types.FloatParamType):
            spec = {"type": "number"}
        else:
            spec = {"type": "string"}
        if isinstance(kind, click.Choice):
            spec["enum"] = list(kind.choices)
        for attr, bound in (("min", "minimum"), ("max", "maximum")):
            value = getattr(kind, attr, None)
            if value is not None:
                if getattr(kind, attr + "_open", False):
                    bound = "exclusive" + bound.capitalize()
                spec[bound] = value
        # Click 8.4 uses an UNSET sentinel for omitted defaults (not JSON data).
        if isinstance(param.default, (str, int, float, bool, list, dict)):
            spec["default"] = param.default
        if getattr(param, "help", None):
            spec["description"] = param.help
        properties[key] = spec
        if param.required:
            required.append(key)
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def command_args(name: str, arguments: dict) -> list[str]:
    """Values cannot become options, even when starting with -- or containing quotes."""
    args, positional = list(TOOL_PATHS[name]), []
    for key, param in parameters(name).items():
        if key not in arguments:
            continue
        value = arguments[key]
        if isinstance(param, click.Argument):
            positional.append(str(value))
        elif param.is_flag:
            if value:
                args.append(param.opts[0])
            elif param.secondary_opts:
                args.append(param.secondary_opts[0])
        else:
            args.append(f"{param.opts[0]}={value}")
    if positional:
        args.extend(["--", *positional])
    return args


async def run_cli(settings: Settings, name: str, arguments: dict, *, deadline: float | None = None) -> dict:
    base = [sys.executable, "-m", "cli_anything.gpt_sovits", "--json"]
    for key in ("checkout", "runtime", "api_url", "tts_config", "state_dir"):
        base.append(f"--{key.replace('_', '-')}={getattr(settings, key)}")
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    process = await asyncio.create_subprocess_exec(
        *base, *command_args(name, arguments),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    timeout_param = parameters(name).get("timeout")
    timeout = arguments.get("timeout", timeout_param.default if timeout_param else 120)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), deadline if deadline is not None else timeout + 60)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        # SDK cancellation uses AnyIO scopes; shield cleanup so the child is reaped.
        import anyio
        with anyio.CancelScope(shield=True):
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.communicate()
        raise
    try:
        result = json.loads(stdout.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise CLIError("invalid_cli_response", "CLI did not return JSON", {"exit_code": process.returncode, "stderr": stderr.decode("utf-8", errors="replace")[-2000:]}) from exc
    if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
        raise CLIError("invalid_cli_response", "CLI returned an invalid envelope")
    if process.returncode and result["ok"]:
        raise CLIError("cli_failed", "CLI exited unsuccessfully", {"exit_code": process.returncode})
    return result


def create_server(settings: Settings):
    from mcp.server.lowlevel import Server
    from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

    server = Server("gpt-sovits", version=__version__, instructions=(
        "Local GPT-SoVITS. Check doctor and serve_status, explicitly start the service, "
        "inspect reference WAV, then synthesize to an absolute local output path. "
        "Mutation tools support dry_run. No automatic training or model switching. "
        "Calls are serialized in this server; do not change models from another client during synthesis. "
        "Stopping this MCP does not stop the separately managed backend; use serve_stop."
    ))
    lock = asyncio.Lock()

    @server.list_tools()
    async def list_tools():
        return [Tool(
            name=name, description=command_for(name).help,
            inputSchema=input_schema(name),
            annotations=ToolAnnotations(readOnlyHint=".".join(path) in READ_ONLY,
                                        destructiveHint=".".join(path) not in READ_ONLY,
                                        openWorldHint=False),
        ) for name, path in TOOL_PATHS.items()]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        try:
            if name not in TOOL_PATHS:
                raise CLIError("unknown_tool", f"Unknown tool: {name}")
            async with lock:
                result = await run_cli(settings, name, arguments)
        except asyncio.TimeoutError:
            result = envelope(name, error={"code": "cli_timeout", "message": "CLI timed out; inspect serve_status before retrying", "details": {}})
        except Exception as exc:
            error = exc if isinstance(exc, CLIError) else CLIError("mcp_execution_failed", str(exc))
            result = envelope(name, error=error.as_dict())
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
                              structuredContent=result, isError=not result["ok"])

    return server


@click.command()
@click.option("--checkout", envvar="GPT_SOVITS_CHECKOUT", required=True, type=click.Path(), help="GPT-SoVITS source checkout (explicit, portable configuration).")
@click.option("--runtime", envvar="GPT_SOVITS_RUNTIME", type=click.Path())
@click.option("--api-url", envvar="GPT_SOVITS_API_URL")
@click.option("--tts-config", envvar="GPT_SOVITS_TTS_CONFIG", type=click.Path())
@click.option("--state-dir", envvar="GPT_SOVITS_STATE_DIR", type=click.Path())
@click.version_option(__version__)
def main(**options):
    """Expose the GPT-SoVITS inference CLI as a local stdio MCP server."""
    try:
        from mcp.server.stdio import stdio_server
    except ImportError as exc:
        raise click.ClickException('Install MCP support: pip install -e ".[mcp]"') from exc
    try:
        settings = Settings.discover(**options)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc

    async def serve():
        server = create_server(settings)
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())

    asyncio.run(serve())


if __name__ == "__main__":
    main()
