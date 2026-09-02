from __future__ import annotations

import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Callable

import yaml

from cli_anything.gpt_sovits.core.config import Settings
from cli_anything.gpt_sovits.core.paths import require_local_path


REQUIRED_TRAINING_SCRIPTS = (
    "GPT_SoVITS/prepare_datasets/1-get-text.py",
    "GPT_SoVITS/prepare_datasets/2-get-hubert-wav32k.py",
    "GPT_SoVITS/prepare_datasets/2-get-sv.py",
    "GPT_SoVITS/prepare_datasets/3-get-semantic.py",
    "GPT_SoVITS/s1_train.py",
    "GPT_SoVITS/s2_train.py",
)
REQUIRED_V2PROPLUS_ASSETS = (
    "GPT_SoVITS/configs/s1longer-v2.yaml",
    "GPT_SoVITS/configs/s2v2ProPlus.json",
    "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large/config.json",
    "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large/pytorch_model.bin",
    "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large/tokenizer.json",
    "GPT_SoVITS/pretrained_models/chinese-hubert-base/config.json",
    "GPT_SoVITS/pretrained_models/chinese-hubert-base/preprocessor_config.json",
    "GPT_SoVITS/pretrained_models/chinese-hubert-base/pytorch_model.bin",
    "GPT_SoVITS/pretrained_models/s1v3.ckpt",
    "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2ProPlus.pth",
    "GPT_SoVITS/pretrained_models/v2Pro/s2Dv2ProPlus.pth",
)
ASR_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")
MINIMUM_WEIGHT_BYTES = {
    "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large/pytorch_model.bin": 100 * 1024**2,
    "GPT_SoVITS/pretrained_models/chinese-hubert-base/pytorch_model.bin": 100 * 1024**2,
    "GPT_SoVITS/pretrained_models/s1v3.ckpt": 100 * 1024**2,
    "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2ProPlus.pth": 100 * 1024**2,
    "GPT_SoVITS/pretrained_models/v2Pro/s2Dv2ProPlus.pth": 100 * 1024**2,
}
MAX_CONFIG_BYTES = 10 * 1024**2
SPEAKER_VECTOR_MODEL = "GPT_SoVITS/pretrained_models/sv/pretrained_eres2netv2w24s4ep4.ckpt"
SPEAKER_VECTOR_MODEL_BYTES = 107_528_697
SPEAKER_VECTOR_MODEL_SHA256 = "4f5a0bf73c61eb41b174e1bb54e7ee3c83233892be8e0af1f187024e8e581a35"


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def default_asr_cache() -> Path:
    root = Path.home() / ".cache" / "huggingface" / "hub" / "models--Systran--faster-whisper-base"
    reference = root / "refs" / "main"
    if reference.is_file():
        revision = reference.read_text(encoding="utf-8").strip()
        if revision:
            return root / "snapshots" / revision
    snapshots = root / "snapshots"
    candidates = sorted((path for path in snapshots.glob("*") if path.is_dir()), key=lambda path: path.name) if snapshots.is_dir() else []
    return candidates[-1] if candidates else root


def _runtime_probe(runtime: Path) -> dict:
    script = (
        "import json,torch; "
        "print(json.dumps({'cuda':bool(torch.cuda.is_available()),"
        "'gpu':torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,"
        "'torch':torch.__version__}))"
    )
    try:
        process = subprocess.run([str(runtime), "-c", script], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
        if process.returncode != 0:
            return {"ok": False, "error": "runtime_probe_failed", "exit_code": process.returncode}
        return {"ok": True, **json.loads(process.stdout.strip())}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {"ok": False, "error": "runtime_probe_failed"}


def _mapping_has(document: object, keys: tuple[str, ...]) -> bool:
    return isinstance(document, dict) and all(key in document for key in keys)


def _parse_structured(path: Path, *, yaml_format: bool = False) -> object:
    if path.stat().st_size > MAX_CONFIG_BYTES:
        raise ValueError("config_too_large")
    with path.open("r", encoding="utf-8") as stream:
        return yaml.safe_load(stream) if yaml_format else json.load(stream)


def _validate_pytorch_archive(path: Path, minimum_bytes: int) -> tuple[bool, str | None]:
    if path.stat().st_size < minimum_bytes:
        return False, f"weight_too_small:{minimum_bytes}"
    try:
        with path.open("rb") as stream:
            if stream.read(4) != b"PK\x03\x04":
                return False, "invalid_pytorch_archive_magic"
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [info.filename.replace("\\", "/") for info in infos]
            has_pickle = any(name.endswith("/data.pkl") and info.file_size > 0 for name, info in zip(names, infos))
            tensor_entries = [info for name, info in zip(names, infos) if "/data/" in name and info.file_size > 0]
            has_version = any(name.endswith("/version") and info.file_size > 0 for name, info in zip(names, infos))
            if not has_pickle or not tensor_entries or not has_version:
                return False, "incomplete_pytorch_archive"
            if any(info.flag_bits & 0x1 for info in infos):
                return False, "encrypted_pytorch_archive"
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return False, "invalid_pytorch_archive"
    return True, None


def _validate_training_asset(relative: str, path: Path) -> tuple[bool, str | None]:
    if relative == SPEAKER_VECTOR_MODEL:
        if path.stat().st_size != SPEAKER_VECTOR_MODEL_BYTES:
            return False, f"speaker_vector_size_mismatch:{SPEAKER_VECTOR_MODEL_BYTES}"
        if _sha256(path) != SPEAKER_VECTOR_MODEL_SHA256:
            return False, "speaker_vector_sha256_mismatch"
        return True, None
    if relative in MINIMUM_WEIGHT_BYTES:
        return _validate_pytorch_archive(path, MINIMUM_WEIGHT_BYTES[relative])
    try:
        if relative == "GPT_SoVITS/configs/s1longer-v2.yaml":
            document = _parse_structured(path, yaml_format=True)
            valid = _mapping_has(document, ("train", "data", "model", "inference"))
        elif relative == "GPT_SoVITS/configs/s2v2ProPlus.json":
            document = _parse_structured(path)
            valid = _mapping_has(document, ("train", "data", "model", "content_module"))
        elif relative.endswith("chinese-roberta-wwm-ext-large/config.json"):
            document = _parse_structured(path)
            valid = (
                _mapping_has(document, ("model_type", "architectures", "hidden_size"))
                and isinstance(document["architectures"], list)
                and bool(document["architectures"])
                and isinstance(document["hidden_size"], int)
                and document["hidden_size"] > 0
            )
        elif relative.endswith("chinese-roberta-wwm-ext-large/tokenizer.json"):
            document = _parse_structured(path)
            model = document.get("model") if isinstance(document, dict) else None
            valid = (
                _mapping_has(document, ("version", "model"))
                and isinstance(model, dict)
                and isinstance(model.get("type"), str)
                and isinstance(model.get("vocab"), dict)
                and bool(model["vocab"])
            )
        elif relative.endswith("chinese-hubert-base/config.json"):
            document = _parse_structured(path)
            valid = (
                _mapping_has(document, ("model_type", "architectures", "hidden_size"))
                and isinstance(document["architectures"], list)
                and bool(document["architectures"])
                and isinstance(document["hidden_size"], int)
                and document["hidden_size"] > 0
            )
        elif relative.endswith("chinese-hubert-base/preprocessor_config.json"):
            document = _parse_structured(path)
            valid = (
                _mapping_has(document, ("feature_extractor_type", "sampling_rate"))
                and isinstance(document["feature_extractor_type"], str)
                and isinstance(document["sampling_rate"], int)
                and document["sampling_rate"] > 0
            )
        else:
            valid = False
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, yaml.YAMLError, ValueError, TypeError):
        return False, "invalid_or_unparseable_structure"
    return (True, None) if valid else (False, "missing_required_structure")


def training_doctor(
    settings: Settings,
    asr_cache: str | Path | None = None,
    uvr_dir: str | Path | None = None,
    data_dir: str | Path | None = None,
    asset_validator: Callable[[str, Path], tuple[bool, str | None]] | None = None,
) -> dict:
    cache = require_local_path(asr_cache if asr_cache is not None else default_asr_cache(), purpose="asr_cache")
    weights = require_local_path(
        uvr_dir if uvr_dir is not None else settings.checkout / "tools" / "uvr5" / "uvr5_weights",
        purpose="uvr_dir",
    )
    target = require_local_path(data_dir if data_dir is not None else settings.state_dir, purpose="data_dir")
    scripts = [{"path": str(settings.checkout / relative), "exists": (settings.checkout / relative).is_file()} for relative in REQUIRED_TRAINING_SCRIPTS]
    scripts_ok = all(item["exists"] for item in scripts)
    validator = asset_validator or _validate_training_asset
    speaker_vector_script = settings.checkout / "GPT_SoVITS/prepare_datasets/2-get-sv.py"
    speaker_vector_path = settings.checkout / SPEAKER_VECTOR_MODEL
    speaker_vector_validation_error = None
    speaker_vector_model_ready = False
    if speaker_vector_path.is_file() and speaker_vector_path.stat().st_size > 0:
        try:
            speaker_vector_model_ready, speaker_vector_validation_error = validator(SPEAKER_VECTOR_MODEL, speaker_vector_path)
        except Exception:
            speaker_vector_validation_error = "validator_failed"
    speaker_vector_ready = (
        speaker_vector_script.is_file()
        and speaker_vector_model_ready
    )
    asset_items = []
    asset_missing = []
    asset_invalid = []
    for relative in REQUIRED_V2PROPLUS_ASSETS:
        path = settings.checkout / relative
        size = path.stat().st_size if path.is_file() else None
        validation_error = None
        if not path.exists():
            ready = False
            asset_missing.append(relative)
            validation_error = "missing"
        elif not path.is_file():
            ready = False
            asset_invalid.append(relative)
            validation_error = "not_a_file"
        elif not size:
            ready = False
            asset_invalid.append(relative)
            validation_error = "empty_file"
        else:
            try:
                ready, validation_error = validator(relative, path)
            except Exception:
                ready, validation_error = False, "validator_failed"
            if not ready:
                asset_invalid.append(relative)
        asset_items.append(
            {
                "relative_path": relative,
                "path": str(path),
                "ready": ready,
                "size_bytes": size,
                "validation_error": validation_error,
            }
        )
    assets_ok = not asset_missing and not asset_invalid
    runtime = _runtime_probe(settings.runtime) if settings.runtime.is_file() else {"ok": False, "error": "runtime_missing"}
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    asr_missing = [name for name in ASR_FILES if not (cache / name).is_file() or (cache / name).stat().st_size <= 0]
    asr_ok = not asr_missing
    uvr_files = sorted(
        str(path.resolve())
        for path in weights.rglob("*")
        if path.is_file() and path.suffix.lower() in {".pth", ".onnx", ".ckpt"}
    ) if weights.is_dir() else []
    uvr_ok = bool(uvr_files)
    disk_root = target.anchor or str(target)
    disk = shutil.disk_usage(disk_root)
    disk_ok = disk.free >= 5 * 1024**3
    missing: list[str] = []
    if not scripts_ok:
        missing.append("training_scripts")
    if not speaker_vector_ready:
        missing.append("speaker_vector")
    if not assets_ok:
        missing.append("v2proplus_assets")
    if not runtime.get("ok") or not runtime.get("cuda"):
        missing.append("cuda_runtime")
    if not ffmpeg_path or not ffprobe_path:
        missing.append("ffmpeg")
    if not asr_ok:
        missing.append("offline_asr_cache")
    if not disk_ok:
        missing.append("disk_space")
    optional_missing = [] if uvr_ok else ["uvr5_weights"]
    required_ready = scripts_ok and speaker_vector_ready and assets_ok and bool(runtime.get("ok")) and bool(runtime.get("cuda")) and bool(ffmpeg_path) and bool(ffprobe_path) and asr_ok and disk_ok
    return {
        "ready": required_ready,
        "required_ready": required_ready,
        "training_scripts": {"status": "ready" if scripts_ok else "missing_required", "required": True, "items": scripts},
        "speaker_vector": {
            "status": (
                "ready"
                if speaker_vector_ready
                else ("missing_required" if not speaker_vector_script.is_file() or not speaker_vector_path.is_file() else "invalid_required")
            ),
            "required": True,
            "script": str(speaker_vector_script),
            "script_exists": speaker_vector_script.is_file(),
            "path": str(speaker_vector_path),
            "size_bytes": speaker_vector_path.stat().st_size if speaker_vector_path.is_file() else None,
            "validation_error": speaker_vector_validation_error,
        },
        "v2proplus_assets": {
            "status": "ready" if assets_ok else ("missing_required" if asset_missing else "invalid_required"),
            "required": True,
            "target": "v2ProPlus",
            "items": asset_items,
            "missing": asset_missing,
            "invalid": asset_invalid,
        },
        "python_gpu": {"status": "ready" if runtime.get("ok") and runtime.get("cuda") else "missing_required", "required": True, **runtime},
        "ffmpeg": {"status": "ready" if ffmpeg_path and ffprobe_path else "missing_required", "required": True, "ffmpeg": ffmpeg_path, "ffprobe": ffprobe_path},
        "disk": {"status": "ready" if disk_ok else "missing_required", "required": True, "path": str(target), "free_bytes": disk.free},
        "offline_asr": {"status": "ready" if asr_ok else "missing_required", "required": True, "path": str(cache), "missing_files": asr_missing},
        "uvr5": {"status": "ready" if uvr_ok else "missing_optional", "required": False, "path": str(weights), "files": uvr_files},
        "missing": missing,
        "optional_missing": optional_missing,
    }
