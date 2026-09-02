from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from cli_anything.gpt_sovits.core.errors import CLIError


DEFAULT_API_URL = "http://127.0.0.1:9880"


def _expanded(value: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(value)))).resolve()


def _default_runtime(checkout: Path, platform: str | None = None) -> Path:
    platform_name = os.name if platform is None else platform
    if platform_name == "nt":
        return checkout / ".conda" / "python.exe"
    return checkout / ".conda" / "bin" / "python"


def ensure_local_api_url(value: str) -> str:
    parsed = urlparse(value.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.port is None:
        raise CLIError("invalid_api_url", "API 地址必须包含协议、主机和端口，例如 http://127.0.0.1:9880")
    host = parsed.hostname
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = host.lower() == "localhost"
    if not is_loopback:
        raise CLIError("remote_api_forbidden", "阶段一只允许连接本机 API（127.0.0.1、localhost 或 ::1）")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username:
        raise CLIError("invalid_api_url", "API 地址只能包含协议、主机和端口")
    return value.rstrip("/")


@dataclass(frozen=True)
class Settings:
    checkout: Path
    runtime: Path
    api_url: str
    tts_config: Path
    state_dir: Path

    @property
    def api_script(self) -> Path:
        return self.checkout / "api_v2.py"

    @classmethod
    def discover(
        cls,
        checkout: str | None = None,
        runtime: str | None = None,
        api_url: str | None = None,
        tts_config: str | None = None,
        state_dir: str | None = None,
    ) -> "Settings":
        checkout_path = _expanded(checkout or os.environ.get("GPT_SOVITS_CHECKOUT") or (Path.home() / "GPT-SoVITS"))
        runtime_path = _expanded(runtime or os.environ.get("GPT_SOVITS_RUNTIME") or _default_runtime(checkout_path))
        config_path = _expanded(
            tts_config
            or os.environ.get("GPT_SOVITS_TTS_CONFIG", checkout_path / "GPT_SoVITS" / "configs" / "tts_infer.yaml")
        )
        local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        state_path = _expanded(state_dir or os.environ.get("GPT_SOVITS_STATE_DIR", local_app_data / "cli-anything-gpt-sovits"))
        return cls(
            checkout=checkout_path,
            runtime=runtime_path,
            api_url=ensure_local_api_url(api_url or os.environ.get("GPT_SOVITS_API_URL", DEFAULT_API_URL)),
            tts_config=config_path,
            state_dir=state_path,
        )

    def validate_backend(self) -> None:
        missing = [str(path) for path in (self.checkout, self.runtime, self.api_script, self.tts_config) if not path.exists()]
        if missing:
            raise CLIError("backend_missing", "GPT-SoVITS 后端文件不完整", {"missing": missing})
