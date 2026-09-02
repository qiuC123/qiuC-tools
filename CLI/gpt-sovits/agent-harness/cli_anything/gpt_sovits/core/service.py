from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import psutil

from cli_anything.gpt_sovits.core.config import Settings
from cli_anything.gpt_sovits.core.errors import CLIError
from cli_anything.gpt_sovits.core.state import load_json, locked_append_json_line, locked_save_json
from cli_anything.gpt_sovits.utils.gpt_sovits_backend import api_probe


STATE_FILE = "service.json"
LOG_FILE = "api.log"


def _paths(settings: Settings) -> tuple[Path, Path]:
    return settings.state_dir / STATE_FILE, settings.state_dir / LOG_FILE


def runtime_config_path(settings: Settings) -> Path:
    return settings.state_dir / "runtime" / "tts_infer.yaml"


def _copy_runtime_config(settings: Settings) -> Path:
    destination = runtime_config_path(settings)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=".tts_infer.", suffix=".tmp", dir=destination.parent)
    os.close(descriptor)
    try:
        shutil.copyfile(settings.tts_config, temp_name)
        os.replace(temp_name, destination)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return destination


def _expected_command(settings: Settings, config_path: Path | None = None) -> list[str]:
    parsed = urlparse(settings.api_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 9880
    return [
        str(settings.runtime),
        str(settings.api_script),
        "-a",
        host,
        "-p",
        str(port),
        "-c",
        str(config_path or runtime_config_path(settings)),
    ]


def _lifecycle(settings: Settings, event: str, **fields) -> None:
    _, log_path = _paths(settings)
    locked_append_json_line(
        log_path,
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
        },
    )


def _lifecycle_safely(settings: Settings, event: str, **fields) -> None:
    """Best-effort diagnostics for an already-failing path; never mask cleanup."""
    try:
        _lifecycle(settings, event, **fields)
    except Exception:
        pass


def verify_process(record: dict) -> tuple[bool, str]:
    try:
        process = psutil.Process(int(record["pid"]))
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return False, "进程已结束"
        if abs(float(process.create_time()) - float(record["process_create_time"])) > 0.01:
            return False, "PID 已被复用：进程创建时间不匹配"
        actual_exe = Path(process.exe()).resolve()
        expected_exe = Path(record["runtime"]).resolve()
        if actual_exe != expected_exe:
            return False, "PID 对应的可执行文件不匹配"
        actual = [str(item) for item in process.cmdline()]
        expected = [str(item) for item in record["command"]]

        def normalize(command: list[str]) -> list[str]:
            result: list[str] = []
            path_value = False
            for index, token in enumerate(command):
                if index in {0, 1} or path_value:
                    result.append(os.path.normcase(str(Path(token).resolve())))
                    path_value = False
                elif token == "-c":
                    result.append(token)
                    path_value = True
                elif token == "-a":
                    result.append(token)
                elif index > 0 and command[index - 1] == "-a":
                    result.append(token.lower())
                else:
                    result.append(token)
            return result

        if normalize(actual) != normalize(expected):
            return False, "PID 完整命令行或参数顺序与启动记录不匹配"
        return True, "身份匹配"
    except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError, ValueError, OSError) as exc:
        return False, str(exc)


def status(settings: Settings, record_event: bool = False) -> dict:
    state_path, log_path = _paths(settings)
    record = load_json(state_path)
    if not record:
        result = {"managed": False, "running": False, "api": api_probe(settings.api_url), "state_path": str(state_path), "log_path": str(log_path)}
    else:
        verified, reason = verify_process(record)
        result = {
            "managed": True,
            "running": verified,
            "identity_verified": verified,
            "identity_reason": reason,
            "pid": record.get("pid"),
            "api": api_probe(settings.api_url),
            "state_path": str(state_path),
            "log_path": str(log_path),
            "record": record,
        }
    if record_event:
        _lifecycle(
            settings,
            "health",
            managed=bool(result.get("managed")),
            running=bool(result.get("running")),
            identity_verified=bool(result.get("identity_verified")),
            api_reachable=bool(result.get("api", {}).get("reachable")),
            api_status=result.get("api", {}).get("status"),
        )
    return result


def start(settings: Settings, timeout: float = 300.0, dry_run: bool = False) -> dict:
    settings.validate_backend()
    current = status(settings)
    if current["managed"] and current["running"]:
        return {"action": "already_running", **current}
    if current["api"].get("reachable"):
        raise CLIError("port_in_use", "目标 API 地址已有服务，但它不属于本 CLI；不会接管", {"api_url": settings.api_url})
    config_copy = runtime_config_path(settings)
    command = _expected_command(settings, config_copy)
    state_path, log_path = _paths(settings)
    plan = {
        "action": "start",
        "command": command,
        "output_policy": "discard",
        "cwd": str(settings.checkout),
        "log_path": str(log_path),
        "state_path": str(state_path),
    }
    if dry_run:
        return {"dry_run": True, **plan}
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    _copy_runtime_config(settings)
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    child_env = os.environ.copy()
    child_env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"})
    _lifecycle(
        settings,
        "start_requested",
        runtime=str(settings.runtime),
        api_script=str(settings.api_script),
        bind_addr=urlparse(settings.api_url).hostname,
        port=urlparse(settings.api_url).port,
        runtime_config=str(config_copy),
        output_policy="discard",
    )
    try:
        process = subprocess.Popen(
            command,
            cwd=settings.checkout,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            env=child_env,
        )
    except Exception as exc:
        _lifecycle_safely(settings, "spawn_failed", component="backend", advice="检查运行环境和文件权限")
        raise CLIError("service_start_failed", "无法启动 GPT-SoVITS API 进程", {"advice": "检查运行环境和文件权限"}) from exc
    parsed = urlparse(settings.api_url)
    try:
        process_create_time = psutil.Process(process.pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
        _terminate_spawned(process)
        _lifecycle_safely(settings, "start_failed", pid=process.pid, reason="process_identity_unavailable")
        raise CLIError("service_start_failed", "无法记录新服务的进程创建时间", {"pid": process.pid}) from exc
    record = {
        "schema": 1,
        "pid": process.pid,
        "runtime": str(settings.runtime),
        "api_script": str(settings.api_script),
        "tts_config": str(config_copy),
        "checkout": str(settings.checkout),
        "api_url": settings.api_url,
        "bind_addr": parsed.hostname,
        "port": parsed.port,
        "command": command,
        "process_create_time": process_create_time,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        locked_save_json(state_path, record)
    except Exception as exc:
        _terminate_spawned(process)
        _lifecycle_safely(settings, "state_save_failed", pid=process.pid, advice="检查状态目录权限和磁盘空间")
        raise CLIError("state_save_failed", "服务状态保存失败；新进程已清理", {"pid": process.pid, "reason": str(exc)}) from exc
    try:
        _lifecycle(settings, "spawned", pid=process.pid, output_policy="discard")
    except Exception as exc:
        _terminate_spawned(process)
        state_path.unlink(missing_ok=True)
        raise CLIError("lifecycle_log_failed", "安全生命周期日志写入失败；新进程已清理", {"advice": "检查状态目录权限和磁盘空间"}) from exc
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            state_path.unlink(missing_ok=True)
            _lifecycle_safely(settings, "backend_exited_before_ready", pid=process.pid, exit_code=process.returncode, advice="检查运行环境、模型文件和 GPU 可用性")
            raise CLIError("service_start_failed", "GPT-SoVITS API 在就绪前退出", {"exit_code": process.returncode, "log_path": str(log_path)})
        probe = api_probe(settings.api_url, timeout=2)
        if probe.get("reachable"):
            try:
                _lifecycle(settings, "service_ready", pid=process.pid, port=parsed.port, api_status=probe.get("status"))
            except Exception as exc:
                _terminate_spawned(process)
                state_path.unlink(missing_ok=True)
                raise CLIError("lifecycle_log_failed", "安全生命周期日志写入失败；新进程已清理", {"advice": "检查状态目录权限和磁盘空间"}) from exc
            return {**plan, "pid": process.pid, "ready": True, "api": probe}
        time.sleep(1)
    _terminate_spawned(process)
    state_path.unlink(missing_ok=True)
    _lifecycle_safely(settings, "start_timeout", pid=process.pid, timeout=timeout, advice="增加启动超时或检查本机 GPU 与模型")
    raise CLIError("service_start_timeout", "等待 GPT-SoVITS API 就绪超时；本次启动的进程已清理", {"pid": process.pid, "log_path": str(log_path), "timeout": timeout})


def _terminate_spawned(process: subprocess.Popen) -> None:
    """Best-effort termination of a newly spawned process and its descendants."""
    try:
        root = psutil.Process(process.pid)
        targets = root.children(recursive=True) + [root]
        for target in targets:
            try:
                target.terminate()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(targets, timeout=10)
        for target in alive:
            try:
                target.kill()
            except psutil.Error:
                pass
        psutil.wait_procs(alive, timeout=5)
        return
    except psutil.Error:
        pass
    try:
        process.terminate()
        process.wait(timeout=10)
    except Exception:
        try:
            process.kill()
            process.wait(timeout=5)
        except Exception:
            pass


def stop(settings: Settings, timeout: float = 20.0, dry_run: bool = False) -> dict:
    state_path, log_path = _paths(settings)
    record = load_json(state_path)
    if not record:
        raise CLIError("not_managed", "没有找到本 CLI 的服务启动记录；不会按端口盲目停止进程")
    verified, reason = verify_process(record)
    if not verified:
        raise CLIError("identity_mismatch", "服务进程身份校验失败；已拒绝停止", {"pid": record.get("pid"), "reason": reason})
    plan = {"action": "stop", "pid": record["pid"], "identity_verified": True, "state_path": str(state_path), "log_path": str(log_path)}
    if dry_run:
        return {"dry_run": True, **plan}
    _lifecycle(settings, "stop_requested", pid=record["pid"])
    process = psutil.Process(int(record["pid"]))
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except psutil.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    state_path.unlink(missing_ok=True)
    _lifecycle(settings, "service_stopped", pid=record["pid"])
    return {**plan, "stopped": True}


def logs(settings: Settings, lines: int = 80) -> dict:
    _, log_path = _paths(settings)
    if not log_path.exists():
        raise CLIError("log_not_found", "服务日志尚不存在", {"path": str(log_path)})
    content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return {"path": str(log_path), "lines": content[-lines:]}
