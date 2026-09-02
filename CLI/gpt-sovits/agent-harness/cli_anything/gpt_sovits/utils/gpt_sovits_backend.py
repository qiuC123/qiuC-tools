from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from cli_anything.gpt_sovits.core.audio import inspect_wav
from cli_anything.gpt_sovits.core.errors import CLIError


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _error_body(raw: bytes) -> object:
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return raw.decode("utf-8", errors="replace")[:2000]


def api_probe(api_url: str, timeout: float = 2.0) -> dict:
    try:
        with _OPENER.open(f"{api_url}/openapi.json", timeout=timeout) as response:
            return {"reachable": response.status == 200, "status": response.status}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"reachable": False, "error": str(exc)}


def api_get(api_url: str, endpoint: str, params: dict, timeout: float = 60.0) -> dict:
    url = f"{api_url}{endpoint}?{urllib.parse.urlencode(params)}"
    try:
        with _OPENER.open(url, timeout=timeout) as response:
            raw = response.read()
            if response.status != 200:
                raise CLIError("api_error", "GPT-SoVITS API 返回失败", {"status": response.status, "body": _error_body(raw)})
            return {"status": response.status, "response": raw.decode("utf-8", errors="replace")}
    except urllib.error.HTTPError as exc:
        raise CLIError("api_error", "GPT-SoVITS API 返回失败", {"status": exc.code, "body": _error_body(exc.read())}) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CLIError("api_unreachable", "无法连接 GPT-SoVITS API", {"reason": str(exc), "api_url": api_url}) from exc


def synthesize(api_url: str, payload: dict, output: str | Path, overwrite: bool, timeout: float) -> dict:
    output_path = Path(output).resolve()
    if output_path.exists() and not overwrite:
        raise CLIError("output_exists", "输出文件已存在；如需覆盖请加 --overwrite", {"path": str(output_path)})
    if not output_path.parent.exists():
        raise CLIError("output_directory_missing", "输出目录不存在", {"path": str(output_path.parent)})
    request = urllib.request.Request(
        f"{api_url}/tts",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "audio/wav"},
        method="POST",
    )
    temp_path: Path | None = None
    try:
        try:
            response = _OPENER.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            raise CLIError("synthesis_failed", "GPT-SoVITS 合成失败", {"status": exc.code, "body": _error_body(exc.read())}) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CLIError("api_unreachable", "无法连接 GPT-SoVITS API", {"reason": str(exc), "api_url": api_url}) from exc
        with response:
            content_type = response.headers.get_content_type().lower()
            if response.status != 200 or not content_type.startswith("audio/"):
                raw = response.read()
                raise CLIError(
                    "invalid_api_response",
                    "API 没有返回音频，已拒绝保存",
                    {"status": response.status, "content_type": content_type, "body": _error_body(raw)},
                )
            descriptor, temp_name = tempfile.mkstemp(prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent)
            os.close(descriptor)
            temp_path = Path(temp_name)
            with temp_path.open("wb") as stream:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
        report = inspect_wav(temp_path, require_non_silent=True)
        if overwrite:
            os.replace(temp_path, output_path)
        else:
            try:
                reservation = os.open(output_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                reservation_identity = os.fstat(reservation)
                os.close(reservation)
            except FileExistsError as exc:
                raise CLIError(
                    "output_exists",
                    "请求期间输出文件已由其他进程创建；已保留该文件",
                    {"path": str(output_path)},
                ) from exc
            try:
                os.replace(temp_path, output_path)
            except Exception:
                try:
                    current = output_path.stat()
                    if (
                        current.st_size == 0
                        and current.st_ino == reservation_identity.st_ino
                        and current.st_dev == reservation_identity.st_dev
                    ):
                        output_path.unlink()
                except FileNotFoundError:
                    pass
                finally:
                    raise
        temp_path = None
        report = inspect_wav(output_path, require_non_silent=True)
        return {"output": report, "parameters": payload}
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()
