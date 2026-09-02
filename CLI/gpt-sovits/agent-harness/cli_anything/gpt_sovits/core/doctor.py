from __future__ import annotations

import json
import shutil
import subprocess

import yaml

from cli_anything.gpt_sovits.core.config import Settings
from cli_anything.gpt_sovits.core.models import list_models
from cli_anything.gpt_sovits.core.service import status


def _check(path) -> dict:
    return {"ok": path.exists(), "path": str(path)}


def run_doctor(settings: Settings) -> dict:
    report = {
        "checkout": _check(settings.checkout),
        "api_script": _check(settings.api_script),
        "runtime": _check(settings.runtime),
        "tts_config": _check(settings.tts_config),
        "ffmpeg": {"ok": bool(shutil.which("ffmpeg")), "path": shutil.which("ffmpeg")},
        "ffprobe": {"ok": bool(shutil.which("ffprobe")), "path": shutil.which("ffprobe")},
        "api_url": settings.api_url,
        "service": status(settings),
    }
    models = list_models(settings)
    report["models"] = {"ok": bool(models["gpt"] and models["sovits"]), "gpt_count": len(models["gpt"]), "sovits_count": len(models["sovits"])}
    if settings.tts_config.exists():
        try:
            content = yaml.safe_load(settings.tts_config.read_text(encoding="utf-8")) or {}
            selected = content.get("custom", {})
            model_checks = {}
            for key in ("bert_base_path", "cnhuhbert_base_path", "t2s_weights_path", "vits_weights_path"):
                value = selected.get(key)
                candidate = settings.checkout / value if value and not str(value).startswith(("/", "\\")) and ":" not in str(value) else value
                model_checks[key] = {"ok": bool(candidate and __import__("pathlib").Path(candidate).exists()), "path": str(candidate) if candidate else None}
            report["configured_models"] = {"ok": all(item["ok"] for item in model_checks.values()), "items": model_checks}
        except Exception as exc:
            report["configured_models"] = {"ok": False, "error": str(exc)}
    if settings.runtime.exists():
        code = "import json,torch; print(json.dumps({'python':__import__('sys').version.split()[0],'torch':torch.__version__,'cuda':torch.cuda.is_available(),'gpu':torch.cuda.get_device_name(0) if torch.cuda.is_available() else None}))"
        try:
            proc = subprocess.run([str(settings.runtime), "-c", code], capture_output=True, text=True, timeout=60, cwd=settings.checkout)
            if proc.returncode == 0:
                runtime_report = json.loads(proc.stdout.strip())
                expected_device = "cpu"
                if settings.tts_config.exists():
                    expected_device = str((yaml.safe_load(settings.tts_config.read_text(encoding="utf-8")) or {}).get("custom", {}).get("device", "cpu")).lower()
                device_ok = not expected_device.startswith("cuda") or bool(runtime_report.get("cuda"))
                report["python_gpu"] = {"ok": device_ok, "expected_device": expected_device, **runtime_report}
            else:
                report["python_gpu"] = {"ok": False, "stderr": proc.stderr.strip()[-2000:]}
        except Exception as exc:
            report["python_gpu"] = {"ok": False, "error": str(exc)}
    else:
        report["python_gpu"] = {"ok": False, "error": "Python runtime missing"}
    report.setdefault("configured_models", {"ok": False, "error": "TTS config missing"})
    checks = [report[key]["ok"] for key in ("checkout", "api_script", "runtime", "tts_config", "ffmpeg", "ffprobe", "models", "configured_models", "python_gpu")]
    report["ready"] = all(checks)
    report["repairs"] = [] if report["ready"] else ["请根据 ok=false 的项目补齐 GPT-SoVITS 源码、运行环境、模型或 FFmpeg。"]
    return report
