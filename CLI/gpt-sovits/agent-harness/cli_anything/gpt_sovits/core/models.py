from __future__ import annotations

from pathlib import Path

from cli_anything.gpt_sovits.core.config import Settings
from cli_anything.gpt_sovits.core.errors import CLIError
from cli_anything.gpt_sovits.core.service import runtime_config_path, status
from cli_anything.gpt_sovits.utils.gpt_sovits_backend import api_get


def list_models(settings: Settings) -> dict:
    roots = [settings.checkout / "GPT_weights_v2", settings.checkout / "SoVITS_weights_v2", settings.checkout / "GPT_SoVITS" / "pretrained_models"]
    gpt: set[str] = set()
    sovits: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() == ".ckpt":
                gpt.add(str(path.resolve()))
            elif path.is_file() and path.suffix.lower() == ".pth":
                sovits.add(str(path.resolve()))
    return {"gpt": sorted(gpt), "sovits": sorted(sovits), "roots": [str(root) for root in roots]}


def validate_weight(path: str, kind: str) -> Path:
    weight = Path(path).resolve()
    suffix = ".ckpt" if kind == "gpt" else ".pth"
    if not weight.is_file():
        raise CLIError("weight_not_found", "找不到模型权重", {"path": str(weight)})
    if weight.suffix.lower() != suffix:
        raise CLIError("wrong_weight_type", f"{kind} 权重应使用 {suffix} 文件", {"path": str(weight)})
    return weight


def use_weight(settings: Settings, path: str, kind: str, dry_run: bool, timeout: float = 120.0) -> dict:
    weight = validate_weight(path, kind)
    endpoint = "/set_gpt_weights" if kind == "gpt" else "/set_sovits_weights"
    plan = {"action": f"use_{kind}", "path": str(weight), "endpoint": endpoint, "api_url": settings.api_url}
    if dry_run:
        return {"dry_run": True, **plan}
    service = status(settings)
    record = service.get("record") or {}
    expected_config = runtime_config_path(settings).resolve()
    try:
        recorded_config = Path(record["tts_config"]).resolve()
    except (KeyError, TypeError):
        recorded_config = None
    managed_for_settings = bool(
        service.get("managed")
        and service.get("running")
        and service.get("identity_verified")
        and record.get("api_url") == settings.api_url
        and recorded_config == expected_config
    )
    if not managed_for_settings:
        raise CLIError(
            "managed_service_required",
            "模型切换只允许操作由本 CLI 启动并通过身份校验的服务，以免写入上游配置",
            {
                "api_url": settings.api_url,
                "state_path": service.get("state_path"),
                "expected_config": str(expected_config),
                "identity_reason": service.get("identity_reason"),
            },
        )
    response = api_get(settings.api_url, endpoint, {"weights_path": str(weight)}, timeout=timeout)
    return {**plan, "backend": response}
