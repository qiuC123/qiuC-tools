from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import yaml

from cli_anything.gpt_sovits.core.config import Settings
from cli_anything.gpt_sovits.core.errors import CLIError
from cli_anything.gpt_sovits.core.paths import require_local_path


DEFAULT_SPEAKER = "speaker"
TARGET_VERSION = "v2ProPlus"
PLAN_SCHEMA = "cli-anything-gpt-sovits/phase2b-plan-v2"
LEGACY_PLAN_SCHEMA = "cli-anything-gpt-sovits/phase2b-plan-v1"
ProcessRunner = Callable[..., int]
CheckpointInspector = Callable[[Path], dict]
GPT_RESUME_LAUNCHER = r'''from __future__ import annotations

import hashlib
import os
import pathlib
import runpy
import sys


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if len(sys.argv) < 9 or sys.argv[1] != "--resume-path" or sys.argv[3] != "--resume-sha256" or sys.argv[5] != "--script" or sys.argv[7] != "--":
    raise SystemExit("invalid controlled GPT resume launcher arguments")
resume_path = pathlib.Path(sys.argv[2]).resolve(strict=True)
expected_sha256 = sys.argv[4]
script_path = pathlib.Path(sys.argv[6]).resolve(strict=True)
if _sha256(resume_path) != expected_sha256:
    raise SystemExit("trusted GPT resume checkpoint hash changed before launch")
os.environ.pop("TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD", None)
os.environ.pop("TORCH_FORCE_WEIGHTS_ONLY_LOAD", None)

import torch

original_load = torch.load


def controlled_load(source, *args, **kwargs):
    try:
        candidate = pathlib.Path(source).resolve(strict=True) if isinstance(source, (str, os.PathLike)) else None
    except OSError:
        candidate = None
    if candidate == resume_path:
        if _sha256(resume_path) != expected_sha256:
            raise RuntimeError("trusted GPT resume checkpoint hash changed during load")
        with torch.serialization.safe_globals([pathlib.WindowsPath]):
            return original_load(source, *args, **kwargs)
    return original_load(source, *args, **kwargs)


torch.load = controlled_load
sys.argv = [str(script_path), *sys.argv[8:]]
runpy.run_path(str(script_path), run_name="__main__")
'''
ALLOWED_UPSTREAM_UNTRACKED = {
    "GPT_SoVITS/text/ja_userdic/user.dict": {
        "size_bytes": 21_321_666,
        "sha256": "b44817ce96e24be7bcfdd009d834b5237fe044dc9ed5f2f9709f71da9d506fed",
    },
    "GPT_SoVITS/text/ja_userdic/userdict.md5": {
        "size_bytes": 32,
        "sha256": "0e38a710261d8fecfac516faa44893ef3d4a315d4bbf5ff789ce2fa19bba1d91",
    },
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="ascii", newline="\n") as stream:
            stream.write(text)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _semantic_preprocess_config(s2: dict) -> dict:
    document = deepcopy(s2)
    model = document.get("model")
    if not isinstance(model, dict):
        raise CLIError("training_config_invalid", "SoVITS 配置缺少 model 对象")
    model.pop("version", None)
    return document


def _serialize_configs_ascii(s1: dict, s2: dict) -> tuple[str, str, str]:
    s2_text = json.dumps(s2, ensure_ascii=True, indent=2) + "\n"
    s2_preprocess_text = json.dumps(_semantic_preprocess_config(s2), ensure_ascii=True, indent=2) + "\n"
    s1_text = yaml.safe_dump(s1, allow_unicode=False, sort_keys=False)
    s1_text.encode("ascii")
    s2_text.encode("ascii")
    s2_preprocess_text.encode("ascii")
    return s1_text, s2_text, s2_preprocess_text


def _normalize_configs_ascii(workspace: Path) -> None:
    s1_path = workspace / "configs" / "s1.yaml"
    s2_path = workspace / "configs" / "s2.json"
    try:
        s1 = yaml.safe_load(s1_path.read_text(encoding="utf-8"))
        s2 = json.loads(s2_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError, json.JSONDecodeError) as exc:
        raise CLIError("training_config_invalid", "隔离训练配置无法解析") from exc
    s1_text, s2_text, s2_preprocess_text = _serialize_configs_ascii(s1, s2)
    _atomic_text(s1_path, s1_text)
    _atomic_text(s2_path, s2_text)
    _atomic_text(workspace / "configs" / "s2-preprocess.json", s2_preprocess_text)


def _read_upstream_status(checkout: Path) -> list[str]:
    process = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=checkout,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if process.returncode != 0:
        raise CLIError("upstream_status_failed", "无法读取上游 Git 状态", {"exit_code": process.returncode})
    return [line for line in process.stdout.splitlines() if line]


def _assert_allowed_upstream_status(
    settings: Settings,
    status_reader: Callable[[], list[str]] | None = None,
) -> list[dict]:
    lines = (status_reader or (lambda: _read_upstream_status(settings.checkout)))()
    allowed_lines = {f"?? {path}" for path in ALLOWED_UPSTREAM_UNTRACKED}
    unexpected = [line for line in lines if line.replace("\\", "/") not in allowed_lines]
    if unexpected:
        raise CLIError("upstream_changed", "检测到未获准的上游 Git 变化", {"unexpected": unexpected})
    evidence = []
    normalized_lines = {line.replace("\\", "/") for line in lines}
    for relative, expected in ALLOWED_UPSTREAM_UNTRACKED.items():
        if f"?? {relative}" not in normalized_lines:
            continue
        path = settings.checkout / relative
        if not path.is_file() or path.stat().st_size != expected["size_bytes"] or _sha256(path) != expected["sha256"]:
            raise CLIError("upstream_cache_changed", "获准的日语字典缓存大小或哈希发生变化", {"path": str(path)})
        evidence.append({"path": str(path), **expected})
    return evidence


def _load_jsonl(path: Path) -> list[dict]:
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CLIError("invalid_manifest", "训练 manifest 不是有效 UTF-8 JSONL", {"path": str(path)}) from exc
    if not rows or not all(isinstance(row, dict) for row in rows):
        raise CLIError("invalid_manifest", "训练 manifest 不能为空且每行必须是对象", {"path": str(path)})
    return rows


def _safe_label_value(value: object, *, field: str) -> str:
    text = str(value or "")
    if not text.strip() or any(token in text for token in ("|", "\r", "\n", "\0")):
        raise CLIError("unsafe_training_label", "训练标签包含空值或不安全分隔符", {"field": field})
    return text


def _safe_identifier(value: object, *, field: str, default: str | None = None) -> str:
    text = str(value if value is not None else default or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", text):
        raise CLIError("invalid_training_identity", "训练身份字段只能包含安全的字母、数字、点、下划线或连字符", {"field": field})
    return text


def _resolve_language(approved: list[dict], language: str | None) -> str:
    if language is not None:
        resolved = _safe_identifier(language, field="language")
        if all(str(row.get(f"text_{resolved}") or "").strip() for row in approved):
            return resolved
        raise CLIError("training_language_missing", "approved 记录缺少所选语言文本", {"field": f"text_{resolved}"})
    candidates = {
        key.removeprefix("text_")
        for row in approved
        for key, value in row.items()
        if key.startswith("text_") and value and all(str(item.get(key) or "").strip() for item in approved)
    }
    if len(candidates) != 1:
        raise CLIError("training_language_required", "无法唯一推断训练语言，请显式传入 --language", {"candidates": sorted(candidates)})
    return _safe_identifier(next(iter(candidates)), field="language")


def _validate_workspace_location(manifest: Path, workspace: Path) -> None:
    manifest_parent = manifest.parent.resolve(strict=True)
    workspace_parent = workspace.parent.resolve(strict=True)
    if workspace_parent != manifest_parent:
        raise CLIError(
            "workspace_boundary",
            "阶段 2B 工作区必须直接位于阶段 2A 数据目录下",
            {"manifest_parent": str(manifest.parent), "workspace": str(workspace)},
        )
    if workspace.name in {"", ".", ".."}:
        raise CLIError("workspace_boundary", "阶段 2B 必须使用独立子目录")


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return path.is_symlink() or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _assert_no_reparse_tree(path: Path, *, stop: Path) -> None:
    current = path
    stop = stop.resolve(strict=True)
    while True:
        if current.exists() and _is_reparse_point(current):
            raise CLIError("workspace_reparse", "阶段 2B 工作区路径包含符号链接或 reparse point", {"path": str(current)})
        if current == stop:
            return
        if current.parent == current:
            raise CLIError("workspace_boundary", "阶段 2B 工作区规范路径越界", {"path": str(path)})
        current = current.parent


def _canonical_member(workspace: Path, path: Path, relative: str, *, must_exist: bool = True) -> Path:
    expected = workspace / relative
    if path != expected:
        raise CLIError(
            "workspace_path_escape",
            "阶段 2B 输出路径必须精确位于隔离工作区内",
            {"expected": str(expected), "actual": str(path)},
        )
    try:
        workspace_resolved = workspace.resolve(strict=True)
        resolved = path.resolve(strict=must_exist)
    except OSError as exc:
        raise CLIError("workspace_path_invalid", "阶段 2B 工作区路径无法规范化", {"path": str(path)}) from exc
    if resolved != workspace_resolved / relative or workspace_resolved not in resolved.parents:
        raise CLIError("workspace_path_escape", "阶段 2B 输出路径解析后逃逸工作区", {"path": str(path)})
    _assert_no_reparse_tree(path, stop=workspace)
    return resolved


def _artifact_record(stage_path: Path, final_path: Path) -> dict:
    return {"path": str(final_path), "sha256": _sha256(stage_path), "size_bytes": stage_path.stat().st_size}


def _validate_plan_identity(workspace: Path, plan: dict, *, allow_legacy: bool = False) -> None:
    schemas = {PLAN_SCHEMA, LEGACY_PLAN_SCHEMA} if allow_legacy else {PLAN_SCHEMA}
    if not isinstance(plan, dict) or plan.get("schema") not in schemas:
        raise CLIError("training_plan_upgrade_required", "阶段 2B 训练计划版本不受执行信任", {"schema": plan.get("schema") if isinstance(plan, dict) else None})
    if plan.get("target") != TARGET_VERSION:
        raise CLIError("training_plan_invalid", "阶段 2B 训练计划身份字段不匹配")
    try:
        _safe_identifier(plan["speaker"], field="speaker")
        _safe_identifier(plan["language"], field="language")
        planned_workspace = Path(plan["workspace"])
        approved_count = int(plan["manifest"]["approved_count"])
        expected_count = int(plan.get("expected_approved_count", approved_count if allow_legacy else 0))
    except (KeyError, TypeError, ValueError) as exc:
        raise CLIError("training_plan_invalid", "阶段 2B 训练计划缺少身份字段") from exc
    if expected_count <= 0 or approved_count != expected_count or planned_workspace != workspace:
        raise CLIError("training_plan_invalid", "阶段 2B 训练计划与工作区不匹配")
    if workspace.exists():
        _assert_no_reparse_tree(workspace, stop=workspace.parent)
        if planned_workspace.resolve(strict=True) != workspace.resolve(strict=True):
            raise CLIError("training_plan_invalid", "阶段 2B 训练计划规范路径不匹配")


def _validate_artifact_hashes(workspace: Path, plan: dict) -> None:
    expected = {
        "approved_manifest": "approved-manifest.jsonl",
        "labels": "training.list",
        "s1_config": "configs/s1.yaml",
        "s2_config": "configs/s2.json",
        "s2_preprocess_config": "configs/s2-preprocess.json",
    }
    artifacts = plan.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != set(expected):
        raise CLIError("training_plan_invalid", "阶段 2B 计划缺少完整隔离产物哈希")
    for name, relative in expected.items():
        item = artifacts.get(name)
        if not isinstance(item, dict):
            raise CLIError("training_plan_invalid", "阶段 2B 隔离产物记录损坏", {"artifact": name})
        path = Path(str(item.get("path", "")))
        _canonical_member(workspace, path, relative)
        actual = _sha256(path)
        if actual != item.get("sha256") or path.stat().st_size != item.get("size_bytes"):
            raise CLIError(
                "training_artifact_hash_mismatch",
                "阶段 2B 隔离产物哈希或大小已篡改",
                {"artifact": name, "path": str(path), "expected": item.get("sha256"), "actual": actual},
            )


def _approved_pretrained_paths(settings: Settings) -> dict[str, Path]:
    root = settings.checkout / "GPT_SoVITS" / "pretrained_models"
    return {
        "gpt": root / "s1v3.ckpt",
        "sovits_generator": root / "v2Pro" / "s2Gv2ProPlus.pth",
        "sovits_discriminator": root / "v2Pro" / "s2Dv2ProPlus.pth",
    }


def _validate_approved_pretrained_path(settings: Settings, path: Path, *, name: str) -> Path:
    expected = _approved_pretrained_paths(settings)[name]
    if path != expected:
        raise CLIError(
            "pretrained_path_changed",
            "隔离配置中的预训练权重路径不是批准路径",
            {"name": name, "expected": str(expected), "actual": str(path)},
        )
    try:
        checkout = settings.checkout.resolve(strict=True)
        pretrained_root = (settings.checkout / "GPT_SoVITS/pretrained_models").resolve(strict=True)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise CLIError("pretrained_missing", "批准的预训练权重无法规范化", {"name": name, "path": str(path)}) from exc
    if resolved != expected.resolve(strict=True) or pretrained_root not in resolved.parents or checkout not in resolved.parents:
        raise CLIError("pretrained_path_changed", "预训练权重解析后逃逸批准的上游目录", {"name": name, "path": str(path)})
    _assert_no_reparse_tree(path, stop=settings.checkout)
    return resolved


def _pretrained_records(settings: Settings) -> dict[str, dict]:
    records = {}
    for name, path in _approved_pretrained_paths(settings).items():
        _validate_approved_pretrained_path(settings, path, name=name)
        records[name] = {
            "path": str(path),
            "size_bytes": path.stat().st_size,
            "sha256": _sha256(path),
            "structure": _pytorch_zip_structure(path, require_modern_metadata=False),
        }
    return records


def _validate_pretrained_models(settings: Settings, plan: dict, s1: dict, s2: dict) -> None:
    records = plan.get("pretrained_models")
    expected_paths = _approved_pretrained_paths(settings)
    if not isinstance(records, dict) or set(records) != set(expected_paths):
        raise CLIError("training_plan_invalid", "阶段 2B 计划缺少完整预训练权重证据")
    configured = {
        "gpt": Path(str(s1.get("pretrained_s1", ""))),
        "sovits_generator": Path(str(s2.get("train", {}).get("pretrained_s2G", ""))),
        "sovits_discriminator": Path(str(s2.get("train", {}).get("pretrained_s2D", ""))),
    }
    for name, path in configured.items():
        resolved = _validate_approved_pretrained_path(settings, path, name=name)
        item = records.get(name)
        if not isinstance(item, dict) or item.get("path") != str(path):
            raise CLIError("pretrained_path_changed", "预训练权重计划路径与配置不一致", {"name": name})
        actual_hash = _sha256(resolved)
        actual_structure = _pytorch_zip_structure(resolved, require_modern_metadata=False)
        if (
            item.get("size_bytes") != resolved.stat().st_size
            or item.get("sha256") != actual_hash
            or item.get("structure") != actual_structure
        ):
            raise CLIError(
                "pretrained_hash_mismatch",
                "批准的预训练权重大小、哈希或安全结构已被替换",
                {"name": name, "path": str(resolved), "actual_sha256": actual_hash},
            )


def _validate_execution_configs(workspace: Path, plan: dict, settings: Settings | None = None) -> None:
    _validate_artifact_hashes(workspace, plan)
    try:
        s1 = yaml.safe_load((workspace / "configs/s1.yaml").read_text(encoding="ascii"))
        s2 = json.loads((workspace / "configs/s2.json").read_text(encoding="ascii"))
        s2_preprocess = json.loads((workspace / "configs/s2-preprocess.json").read_text(encoding="ascii"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError, json.JSONDecodeError) as exc:
        raise CLIError("training_config_invalid", "隔离训练配置无法安全解析") from exc
    expected_parameters = {
        "sovits": {"batch_size": 1, "epochs": 8, "save_every_epoch": 1, "fp16": True},
        "gpt": {"batch_size": 1, "epochs": 10, "save_every_epoch": 1, "precision": "16-mixed"},
    }
    if plan.get("parameters") != expected_parameters:
        raise CLIError("training_parameters_changed", "阶段 2B 固定训练参数已被修改")
    s2_train = s2.get("train", {})
    s1_train = s1.get("train", {})
    if (
        s2.get("version") != TARGET_VERSION
        or s2.get("model", {}).get("version") != TARGET_VERSION
        or s2_train.get("batch_size") != 1
        or s2_train.get("epochs") != 8
        or s2_train.get("save_every_epoch") != 1
        or s2_train.get("fp16_run") is not True
        or s1_train.get("batch_size") != 1
        or s1_train.get("epochs") != 10
        or s1_train.get("save_every_n_epoch") != 1
        or s1_train.get("precision") != "16-mixed"
        or s2.get("name") != plan.get("speaker")
        or s1_train.get("exp_name") != plan.get("speaker")
    ):
        raise CLIError("training_parameters_changed", "隔离配置中的版本、batch、epochs、保存频率或精度已被修改")
    if s2_preprocess != _semantic_preprocess_config(s2):
        raise CLIError("training_config_invalid", "SoVITS 预处理配置与训练配置不一致")
    expected_paths = {
        ("s2", "data.exp_dir"): (Path(s2.get("data", {}).get("exp_dir", "")), "features"),
        ("s2", "s2_ckpt_dir"): (Path(s2.get("s2_ckpt_dir", "")), "internal/sovits"),
        ("s2", "save_weight_dir"): (Path(s2.get("save_weight_dir", "")), "checkpoints/sovits"),
        ("s1", "train.half_weights_save_dir"): (Path(s1_train.get("half_weights_save_dir", "")), "checkpoints/gpt"),
        ("s1", "train_semantic_path"): (Path(s1.get("train_semantic_path", "")), "features/6-name2semantic.tsv"),
        ("s1", "train_phoneme_path"): (Path(s1.get("train_phoneme_path", "")), "features/2-name2text.txt"),
        ("s1", "output_dir"): (Path(s1.get("output_dir", "")), "internal/gpt"),
    }
    for (config_name, field), (path, relative) in expected_paths.items():
        _canonical_member(workspace, path, relative, must_exist=False)
    if settings is not None:
        _validate_pretrained_models(settings, plan, s1, s2)


def _build_configs(settings: Settings, output_root: Path, speaker: str) -> tuple[dict, dict, dict]:
    s2_source = settings.checkout / "GPT_SoVITS" / "configs" / "s2v2ProPlus.json"
    s1_source = settings.checkout / "GPT_SoVITS" / "configs" / "s1longer-v2.yaml"
    if not s2_source.is_file() or not s1_source.is_file():
        raise CLIError("training_template_missing", "V2ProPlus 训练配置模板缺失")
    try:
        s2 = json.loads(s2_source.read_text(encoding="utf-8"))
        s1 = yaml.safe_load(s1_source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise CLIError("training_template_invalid", "V2ProPlus 训练配置模板无法解析") from exc

    features = output_root / "features"
    checkpoints = output_root / "checkpoints"
    s2_train = s2.setdefault("train", {})
    s2_train.update(
        {
            "batch_size": 1,
            "epochs": 8,
            "fp16_run": True,
            "save_every_epoch": 1,
            "if_save_latest": False,
            "if_save_every_weights": True,
            "gpu_numbers": "0",
            "grad_ckpt": False,
            "pretrained_s2G": str(settings.checkout / "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2ProPlus.pth"),
            "pretrained_s2D": str(settings.checkout / "GPT_SoVITS/pretrained_models/v2Pro/s2Dv2ProPlus.pth"),
        }
    )
    s2.setdefault("model", {})["version"] = TARGET_VERSION
    s2.setdefault("data", {})["exp_dir"] = str(features)
    s2["s2_ckpt_dir"] = str(output_root / "internal" / "sovits")
    s2["save_weight_dir"] = str(checkpoints / "sovits")
    s2["name"] = speaker
    s2["version"] = TARGET_VERSION

    s1_train = s1.setdefault("train", {})
    s1_train.update(
        {
            "batch_size": 1,
            "epochs": 10,
            "save_every_n_epoch": 1,
            "precision": "16-mixed",
            "if_save_latest": False,
            "if_save_every_weights": True,
            "if_dpo": False,
            "half_weights_save_dir": str(checkpoints / "gpt"),
            "exp_name": speaker,
        }
    )
    s1["pretrained_s1"] = str(settings.checkout / "GPT_SoVITS/pretrained_models/s1v3.ckpt")
    s1["train_semantic_path"] = str(features / "6-name2semantic.tsv")
    s1["train_phoneme_path"] = str(features / "2-name2text.txt")
    s1["output_dir"] = str(output_root / "internal" / "gpt")
    evidence = {
        "s1_template": {"path": str(s1_source), "sha256": _sha256(s1_source)},
        "s2_template": {"path": str(s2_source), "sha256": _sha256(s2_source)},
    }
    return s1, s2, evidence


def _safe_remove_planning_workspace(workspace: Path, manifest: Path) -> None:
    """Remove only an untouched v2 planning workspace created by this CLI."""
    if not workspace.is_dir() or _is_reparse_point(workspace):
        raise CLIError("unsafe_overwrite", "拒绝覆盖非普通阶段 2B 工作区", {"path": str(workspace)})
    _assert_no_reparse_tree(workspace, stop=workspace.parent)
    plan_path = workspace / "plan.json"
    if not plan_path.is_file() or _is_reparse_point(plan_path):
        raise CLIError("unsafe_overwrite", "拒绝覆盖没有可信阶段 2B 标记的目录", {"path": str(workspace)})
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CLIError("unsafe_overwrite", "阶段 2B 覆盖标记损坏") from exc
    _validate_plan_identity(workspace, plan)
    planned_manifest = Path(str(plan.get("manifest", {}).get("path", "")))
    if planned_manifest != manifest or planned_manifest.resolve(strict=True) != manifest.resolve(strict=True):
        raise CLIError("unsafe_overwrite", "阶段 2B 覆盖计划与 manifest 身份不匹配")
    if _sha256(manifest) != plan.get("manifest", {}).get("sha256"):
        raise CLIError("unsafe_overwrite", "阶段 2B 覆盖计划的 manifest 哈希不匹配")
    _validate_artifact_hashes(workspace, plan)
    allowed_files = {
        "plan.json",
        "training.list",
        "approved-manifest.jsonl",
        "configs/s1.yaml",
        "configs/s2.json",
        "configs/s2-preprocess.json",
    }
    actual_files = set()
    for path in workspace.rglob("*"):
        if _is_reparse_point(path):
            raise CLIError("unsafe_overwrite", "阶段 2B 覆盖目录包含符号链接或 reparse point", {"path": str(path)})
        if path.is_file():
            actual_files.add(path.relative_to(workspace).as_posix())
    unknown = sorted(actual_files - allowed_files)
    if unknown:
        raise CLIError("unsafe_overwrite", "阶段 2B 覆盖目录包含非白名单或训练产物", {"unknown": unknown})
    for relative in sorted(allowed_files, key=lambda item: item.count("/"), reverse=True):
        path = workspace / relative
        if path.is_file():
            path.unlink()
    for path in sorted((item for item in workspace.rglob("*") if item.is_dir()), key=lambda item: len(item.parts), reverse=True):
        path.rmdir()
    workspace.rmdir()


def prepare_training_workspace(
    settings: Settings,
    manifest_path: str | Path,
    workspace_path: str | Path,
    expected_manifest_sha256: str,
    *,
    dry_run: bool = False,
    overwrite: bool = False,
    speaker: str = DEFAULT_SPEAKER,
    language: str | None = None,
    expected_approved_count: int | None = None,
) -> dict:
    manifest = require_local_path(manifest_path, purpose="training_manifest")
    workspace = require_local_path(workspace_path, purpose="training_workspace")
    _validate_workspace_location(manifest, workspace)
    if not manifest.is_file():
        raise CLIError("manifest_missing", "阶段 2A 正式 manifest 不存在", {"path": str(manifest)})
    actual_manifest_sha = _sha256(manifest)
    if actual_manifest_sha.lower() != str(expected_manifest_sha256).strip().lower():
        raise CLIError(
            "manifest_hash_mismatch",
            "manifest SHA-256 与批准基线不一致",
            {"expected": expected_manifest_sha256, "actual": actual_manifest_sha},
        )
    if workspace.exists() and not overwrite:
        raise CLIError("output_exists", "阶段 2B 工作区已存在，默认拒绝覆盖", {"path": str(workspace)})
    rows = _load_jsonl(manifest)
    approved = [row for row in rows if row.get("review_status") == "approved"]
    resolved_speaker = _safe_identifier(speaker, field="speaker", default=DEFAULT_SPEAKER)
    resolved_language = _resolve_language(approved, language)
    resolved_count = len(approved) if expected_approved_count is None else int(expected_approved_count)
    if resolved_count <= 0 or len(approved) != resolved_count:
        raise CLIError(
            "approved_count_mismatch",
            f"approved 记录数量必须与预期 {resolved_count} 条一致",
            {"expected": resolved_count, "actual": len(approved)},
        )

    snapshot = []
    labels = []
    seen_audio: set[str] = set()
    for index, row in enumerate(approved, start=1):
        if row.get("processing") != "original":
            raise CLIError("non_original_approved", "approved 训练记录必须全部为 original", {"index": index})
        audio = require_local_path(_safe_label_value(row.get("audio_path"), field="audio_path"), purpose="approved_audio")
        text_field = f"text_{resolved_language}"
        text = _safe_label_value(row.get(text_field), field=text_field)
        if not audio.is_file():
            raise CLIError("approved_audio_missing", "approved 音频不存在", {"path": str(audio)})
        if str(audio).casefold() in seen_audio:
            raise CLIError("duplicate_approved_audio", "approved 音频路径重复", {"path": str(audio)})
        seen_audio.add(str(audio).casefold())
        actual_audio_sha = _sha256(audio)
        expected_audio_sha = str(row.get("sha256") or "").lower()
        if actual_audio_sha != expected_audio_sha:
            raise CLIError(
                "audio_hash_mismatch",
                "approved WAV 的 SHA-256 已变化",
                {"path": str(audio), "expected": expected_audio_sha, "actual": actual_audio_sha},
            )
        labels.append(f"{audio}|{resolved_speaker}|{resolved_language}|{text}")
        snapshot.append({**row, "audio_path": str(audio), "sha256": actual_audio_sha})

    if dry_run:
        return {
            "dry_run": True,
            "workspace": str(workspace),
            "manifest_sha256": actual_manifest_sha,
            "approved_count": len(snapshot),
            "would_write": [
                "training.list",
                "approved-manifest.jsonl",
                "configs/s1.yaml",
                "configs/s2.json",
                "configs/s2-preprocess.json",
                "plan.json",
            ],
        }
    if workspace.exists() and overwrite:
        _safe_remove_planning_workspace(workspace, manifest)

    stage = workspace.parent / f".{workspace.name}.stage-{os.getpid()}"
    if stage.exists():
        raise CLIError("workspace_stage_exists", "临时工作区冲突", {"path": str(stage)})
    try:
        (stage / "configs").mkdir(parents=True)
        for relative in ("features", "logs", "checkpoints/sovits", "checkpoints/gpt", "internal"):
            (stage / relative).mkdir(parents=True, exist_ok=True)
        s1, s2, template_evidence = _build_configs(settings, workspace, resolved_speaker)
        (stage / "training.list").write_text("\n".join(labels) + "\n", encoding="utf-8", newline="\n")
        (stage / "approved-manifest.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in snapshot),
            encoding="utf-8",
            newline="\n",
        )
        s1_text, s2_text, s2_preprocess_text = _serialize_configs_ascii(s1, s2)
        (stage / "configs" / "s2.json").write_text(s2_text, encoding="ascii")
        (stage / "configs" / "s2-preprocess.json").write_text(s2_preprocess_text, encoding="ascii")
        (stage / "configs" / "s1.yaml").write_text(s1_text, encoding="ascii")
        artifacts = {
            "approved_manifest": _artifact_record(stage / "approved-manifest.jsonl", workspace / "approved-manifest.jsonl"),
            "labels": _artifact_record(stage / "training.list", workspace / "training.list"),
            "s1_config": _artifact_record(stage / "configs/s1.yaml", workspace / "configs/s1.yaml"),
            "s2_config": _artifact_record(stage / "configs/s2.json", workspace / "configs/s2.json"),
            "s2_preprocess_config": _artifact_record(
                stage / "configs/s2-preprocess.json", workspace / "configs/s2-preprocess.json"
            ),
        }
        plan = {
            "schema": PLAN_SCHEMA,
            "created_at": datetime.now(timezone.utc).astimezone().isoformat(),
            "target": TARGET_VERSION,
            "speaker": resolved_speaker,
            "language": resolved_language,
            "expected_approved_count": resolved_count,
            "workspace": str(workspace),
            "manifest": {"path": str(manifest), "sha256": actual_manifest_sha, "approved_count": len(snapshot)},
            "approved_audio": [{"path": row["audio_path"], "sha256": row["sha256"]} for row in snapshot],
            "artifacts": artifacts,
            "pretrained_models": _pretrained_records(settings),
            "templates": template_evidence,
            "parameters": {
                "sovits": {"batch_size": 1, "epochs": 8, "save_every_epoch": 1, "fp16": True},
                "gpt": {"batch_size": 1, "epochs": 10, "save_every_epoch": 1, "precision": "16-mixed"},
            },
        }
        _atomic_json(stage / "plan.json", plan)
        os.rename(stage, workspace)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return {
        "dry_run": False,
        "workspace": str(workspace),
        "manifest_sha256": actual_manifest_sha,
        "approved_count": len(snapshot),
        "label_path": str(workspace / "training.list"),
        "plan_path": str(workspace / "plan.json"),
    }


def _load_plan(
    workspace_path: str | Path,
    *,
    allow_legacy: bool = False,
    settings: Settings | None = None,
) -> tuple[Path, dict]:
    workspace = require_local_path(workspace_path, purpose="training_workspace")
    plan_path = workspace / "plan.json"
    if not plan_path.is_file():
        raise CLIError("training_plan_missing", "阶段 2B 训练计划不存在", {"path": str(plan_path)})
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CLIError("training_plan_invalid", "阶段 2B 训练计划损坏") from exc
    _validate_plan_identity(workspace, plan, allow_legacy=allow_legacy)
    manifest = Path(plan["manifest"]["path"])
    if not manifest.is_file() or _sha256(manifest) != plan["manifest"]["sha256"]:
        raise CLIError("manifest_hash_mismatch", "预处理前 manifest SHA-256 已变化")
    for item in plan["approved_audio"]:
        audio = Path(item["path"])
        if not audio.is_file() or _sha256(audio) != item["sha256"]:
            raise CLIError("audio_hash_mismatch", "预处理前 approved WAV SHA-256 已变化", {"path": str(audio)})
    if plan.get("schema") == PLAN_SCHEMA:
        _validate_execution_configs(workspace, plan, settings)
    return workspace, plan


def _default_runner(command: list[str], *, cwd: Path, env: dict[str, str], log_path: Path) -> int:
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        process = subprocess.run(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, text=True)
    return int(process.returncode)


def _count_nonempty_lines(path: Path) -> int:
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()) if path.is_file() else 0


def _verify_count(path: Path, expected: int, *, artifact: str, directory: bool = False) -> int:
    if directory:
        count = sum(1 for item in path.iterdir() if item.is_file() and item.stat().st_size > 0) if path.is_dir() else 0
    else:
        count = _count_nonempty_lines(path)
    if count < expected:
        raise CLIError("preprocess_output_invalid", f"{artifact} 产物数量不足", {"path": str(path), "expected": expected, "actual": count})
    return count


def _resume_state(features: Path, name: str, expected_count: int) -> tuple[str, int | None]:
    if name == "text":
        final = features / "2-name2text.txt"
        part = features / "2-name2text-0.txt"
        if not final.exists() and not part.exists():
            return "missing", None
        count = _count_nonempty_lines(final)
        if final.is_file() and count == expected_count and not part.exists():
            return "verified", count
        raise CLIError("preprocess_resume_invalid", "已有 text 产物数量或结构无效，拒绝重跑覆盖", {"actual": count})
    if name == "hubert":
        paths = [features / "4-cnhubert", features / "5-wav32k"]
    elif name == "speaker_vector":
        paths = [features / "7-sv_cn"]
    else:
        final = features / "6-name2semantic.tsv"
        part = features / "6-name2semantic-0.tsv"
        if not final.exists() and not part.exists():
            return "missing", None
        count = _count_nonempty_lines(final)
        if final.is_file() and count == expected_count + 1 and not part.exists():
            return "verified", expected_count
        raise CLIError("preprocess_resume_invalid", "已有 semantic 产物数量或结构无效，拒绝重跑覆盖", {"actual": count})
    if not any(path.exists() for path in paths):
        return "missing", None
    counts = [sum(1 for item in path.iterdir() if item.is_file() and item.stat().st_size > 0) if path.is_dir() else 0 for path in paths]
    if all(count == expected_count for count in counts):
        return "verified", expected_count
    raise CLIError("preprocess_resume_invalid", f"已有 {name} 产物数量不足或结构无效，拒绝重跑覆盖", {"counts": counts})


def _next_log_path(workspace: Path, name: str) -> Path:
    base = workspace / "logs" / f"preprocess-{name}.log"
    if not base.exists():
        return base
    index = 1
    while True:
        candidate = workspace / "logs" / f"preprocess-{name}-resume{index}.log"
        if not candidate.exists():
            return candidate
        index += 1


def _next_training_log_path(workspace: Path, target: str) -> Path:
    base = workspace / "logs" / f"train-{target}.log"
    if not base.exists():
        return base
    index = 1
    while True:
        candidate = workspace / "logs" / f"train-{target}-resume{index}.log"
        if not candidate.exists():
            return candidate
        index += 1


def _checkpoint_records(workspace: Path, target: str) -> list[dict]:
    if target == "sovits":
        sources = (
            (workspace / "checkpoints" / "sovits", "*.pth", "lightweight"),
            (workspace / "features" / "logs_s2_v2ProPlus", "G_*.pth", "full_generator"),
            (workspace / "features" / "logs_s2_v2ProPlus", "D_*.pth", "full_discriminator"),
        )
    else:
        sources = (
            (workspace / "checkpoints" / "gpt", "*.ckpt", "lightweight"),
            (workspace / "internal" / "gpt" / "ckpt", "*.ckpt", "full_training"),
        )
    records = []
    for directory, pattern, kind in sources:
        for path in sorted(directory.glob(pattern)):
            if path.is_file() and path.stat().st_size > 0:
                records.append(
                    {
                        "target": target,
                        "kind": kind,
                        "path": str(path),
                        "size_bytes": path.stat().st_size,
                        "sha256": _sha256(path),
                    }
                )
    return records


def _pytorch_zip_structure(path: Path, *, require_modern_metadata: bool = True) -> dict:
    if not path.is_file() or path.stat().st_size <= 0 or not zipfile.is_zipfile(path):
        raise CLIError("checkpoint_structure_invalid", "训练检查点不是非空 PyTorch ZIP 结构", {"path": str(path)})
    try:
        with zipfile.ZipFile(path) as archive:
            names = [name.replace("\\", "/") for name in archive.namelist()]
            if any(name.startswith("/") or ".." in Path(name).parts for name in names):
                raise CLIError("checkpoint_structure_invalid", "训练检查点 ZIP 包含不安全路径", {"path": str(path)})
            required_suffixes = ["/data.pkl", "/version"]
            if require_modern_metadata:
                required_suffixes.extend(["/byteorder", "/.data/serialization_id"])
            missing = [suffix for suffix in required_suffixes if not any(name.endswith(suffix) for name in names)]
            tensor_entries = [name for name in names if re.search(r"/data/\d+$", name)]
            if missing or not tensor_entries:
                raise CLIError(
                    "checkpoint_structure_invalid",
                    "训练检查点缺少 PyTorch 序列化元数据或 tensor 数据",
                    {"path": str(path), "missing": missing, "tensor_entries": len(tensor_entries)},
                )
            bad = archive.testzip()
            if bad is not None:
                raise CLIError("checkpoint_structure_invalid", "训练检查点 ZIP 校验失败", {"path": str(path), "entry": bad})
    except (OSError, zipfile.BadZipFile) as exc:
        raise CLIError("checkpoint_structure_invalid", "训练检查点 ZIP 无法安全读取", {"path": str(path)}) from exc
    return {
        "format": "pytorch-zip",
        "entry_count": len(names),
        "tensor_entries": len(tensor_entries),
        "has_byteorder": any(name.endswith("/byteorder") for name in names),
        "has_serialization_id": any(name.endswith("/.data/serialization_id") for name in names),
    }


def _runtime_checkpoint_inspector(settings: Settings) -> CheckpointInspector:
    script = r'''
import json, pathlib, sys, torch
path = pathlib.Path(sys.argv[1])
safe = [pathlib.WindowsPath, pathlib.PosixPath]
with torch.serialization.safe_globals(safe):
    value = torch.load(path, map_location="cpu", weights_only=True)
if not isinstance(value, dict):
    raise SystemExit("top-level checkpoint is not a dict")
state = value.get("state_dict")
optimizers = value.get("optimizer_states")
print(json.dumps({
    "format": "pytorch-zip",
    "top_level_keys": sorted(str(key) for key in value.keys()),
    "epoch": value.get("epoch"),
    "global_step": value.get("global_step"),
    "state_dict_entries": len(state) if isinstance(state, dict) else 0,
    "optimizer_state_entries": len(optimizers) if isinstance(optimizers, list) else 0,
}))
'''

    def inspect(path: Path) -> dict:
        process = subprocess.run(
            [str(settings.runtime), "-s", "-c", script, str(path)],
            cwd=path.parent,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )
        if process.returncode != 0:
            raise CLIError(
                "checkpoint_metadata_invalid",
                "训练检查点未通过 weights_only 安全元数据检查",
                {"path": str(path), "exit_code": process.returncode, "stderr": process.stderr[-1000:]},
            )
        try:
            metadata = json.loads(process.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise CLIError("checkpoint_metadata_invalid", "训练检查点安全检查未返回有效元数据", {"path": str(path)}) from exc
        return metadata

    return inspect


def _validate_gpt_metadata(path: Path, metadata: dict, *, epoch: int, step: int) -> dict:
    required_keys = {"state_dict", "optimizer_states", "loops", "callbacks", "lr_schedulers"}
    keys = set(metadata.get("top_level_keys", [])) if isinstance(metadata, dict) else set()
    if (
        metadata.get("format") != "pytorch-zip"
        or metadata.get("epoch") != epoch
        or metadata.get("global_step") != step
        or not required_keys.issubset(keys)
        or int(metadata.get("state_dict_entries", 0)) <= 0
        or int(metadata.get("optimizer_state_entries", 0)) <= 0
    ):
        raise CLIError(
            "checkpoint_metadata_invalid",
            "GPT 完整检查点的轮次、步数或训练状态元数据不可信",
            {"path": str(path), "metadata": metadata},
        )
    return metadata


def _trust_context(workspace: Path, plan: dict) -> dict:
    return {
        "plan_sha256": _sha256(workspace / "plan.json"),
        "s1_config_sha256": plan["artifacts"]["s1_config"]["sha256"],
    }


def _record_gpt_resume_trust(
    workspace: Path,
    plan: dict,
    inspector: CheckpointInspector,
    *,
    exit_code: int,
) -> None:
    checkpoint_dir = workspace / "internal" / "gpt" / "ckpt"
    if not checkpoint_dir.is_dir():
        return
    pattern = re.compile(r"^epoch=(\d+)-step=(\d+)\.ckpt$")
    records = []
    for path in sorted(checkpoint_dir.glob("*.ckpt")):
        match = pattern.fullmatch(path.name)
        if match is None:
            raise CLIError("gpt_resume_checkpoint_invalid", "GPT 恢复目录包含未知检查点", {"path": str(path)})
        _canonical_member(workspace, path, f"internal/gpt/ckpt/{path.name}")
        structure = _pytorch_zip_structure(path)
        before_hash = _sha256(path)
        metadata = _validate_gpt_metadata(
            path,
            inspector(path),
            epoch=int(match.group(1)),
            step=int(match.group(2)),
        )
        if _sha256(path) != before_hash:
            raise CLIError("checkpoint_replaced", "GPT 检查点在安全检查期间被替换", {"path": str(path)})
        records.append(
            {
                "path": str(path),
                "name": path.name,
                "size_bytes": path.stat().st_size,
                "sha256": before_hash,
                "structure": structure,
                "metadata": metadata,
            }
        )
    if records:
        _atomic_json(
            workspace / "gpt-resume-trust.json",
            {
                "schema": "cli-anything-gpt-sovits/gpt-resume-trust-v1",
                "source": "failed-cli-training-attempt",
                "recorded_at": datetime.now(timezone.utc).astimezone().isoformat(),
                "exit_code": exit_code,
                **_trust_context(workspace, plan),
                "checkpoints": records,
            },
        )


def _gpt_resume_checkpoint(
    workspace: Path,
    plan: dict,
    inspector: CheckpointInspector,
) -> dict | None:
    checkpoint_dir = workspace / "internal" / "gpt" / "ckpt"
    if not checkpoint_dir.is_dir():
        return None
    candidates = []
    invalid = []
    pattern = re.compile(r"^epoch=(\d+)-step=(\d+)\.ckpt$")
    for path in checkpoint_dir.glob("*.ckpt"):
        match = pattern.fullmatch(path.name)
        if match is None or not path.is_file() or path.stat().st_size <= 0:
            invalid.append(str(path))
            continue
        candidates.append((int(match.group(1)), int(match.group(2)), path))
    if invalid:
        raise CLIError(
            "gpt_resume_checkpoint_invalid",
            "GPT 恢复目录包含空文件或无法识别的完整检查点",
            {"invalid": sorted(invalid)},
        )
    if not candidates:
        return None
    trust_path = workspace / "gpt-resume-trust.json"
    if not trust_path.is_file():
        raise CLIError("gpt_resume_untrusted", "GPT 完整检查点没有本 CLI 记录的预期哈希，拒绝不安全恢复")
    try:
        trust = json.loads(trust_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CLIError("gpt_resume_untrusted", "GPT 检查点信任记录损坏") from exc
    if trust.get("schema") != "cli-anything-gpt-sovits/gpt-resume-trust-v1" or any(
        trust.get(key) != value for key, value in _trust_context(workspace, plan).items()
    ):
        raise CLIError("gpt_resume_untrusted", "GPT 检查点信任记录与当前计划或配置不匹配")
    trusted = {item.get("name"): item for item in trust.get("checkpoints", []) if isinstance(item, dict)}
    if set(trusted) != {path.name for _, _, path in candidates}:
        raise CLIError("gpt_resume_untrusted", "GPT 恢复目录包含未知或缺失检查点")
    verified = []
    for epoch, step, path in candidates:
        _canonical_member(workspace, path, f"internal/gpt/ckpt/{path.name}")
        item = trusted[path.name]
        actual_hash = _sha256(path)
        if item.get("path") != str(path) or item.get("sha256") != actual_hash or item.get("size_bytes") != path.stat().st_size:
            raise CLIError("checkpoint_replaced", "GPT 完整检查点哈希、大小或规范路径已被替换", {"path": str(path)})
        structure = _pytorch_zip_structure(path)
        metadata = _validate_gpt_metadata(path, inspector(path), epoch=epoch, step=step)
        if _sha256(path) != actual_hash or structure != item.get("structure") or metadata != item.get("metadata"):
            raise CLIError("checkpoint_replaced", "GPT 完整检查点结构或元数据已变化", {"path": str(path)})
        verified.append((epoch, step, path, actual_hash, metadata))
    epoch, step, path, actual_hash, metadata = max(verified, key=lambda item: (item[0], item[1]))
    return {
        "path": str(path),
        "name": path.name,
        "epoch": epoch,
        "global_step": step,
        "size_bytes": path.stat().st_size,
        "sha256": actual_hash,
        "metadata": metadata,
        "trust_record": str(trust_path),
    }


def _validate_training_checkpoints(
    target: str,
    checkpoints: list[dict],
    log_path: Path,
    inspector: CheckpointInspector,
    speaker: str,
) -> None:
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    by_kind: dict[str, list[dict]] = {}
    for item in checkpoints:
        by_kind.setdefault(item["kind"], []).append(item)
    if target == "sovits":
        lightweight = {}
        for item in by_kind.get("lightweight", []):
            match = re.fullmatch(rf"{re.escape(speaker)}_e(\d+)_s(\d+)\.pth", Path(item["path"]).name)
            if match:
                lightweight[int(match.group(1))] = (int(match.group(2)), item)
        generators = {
            int(match.group(1)): item
            for item in by_kind.get("full_generator", [])
            if (match := re.fullmatch(r"G_(\d+)\.pth", Path(item["path"]).name))
        }
        discriminators = {
            int(match.group(1)): item
            for item in by_kind.get("full_discriminator", [])
            if (match := re.fullmatch(r"D_(\d+)\.pth", Path(item["path"]).name))
        }
        expected_epochs = set(range(1, 9))
        steps = {step for step, _ in lightweight.values()}
        if (
            len(by_kind.get("lightweight", [])) != 8
            or len(by_kind.get("full_generator", [])) != 8
            or len(by_kind.get("full_discriminator", [])) != 8
            or set(lightweight) != expected_epochs
            or steps != set(generators)
            or steps != set(discriminators)
            or lightweight[8][0] != max(steps)
        ):
            raise CLIError("checkpoint_count_mismatch", "SoVITS 必须恰好完成 8 轮且轻量/G/D 检查点逐轮配对", {"log": str(log_path)})
        for item in checkpoints:
            _pytorch_zip_structure(Path(item["path"]))
        if not re.search(r"====> Epoch:\s*8\b", log_text) or "training done" not in log_text:
            raise CLIError("training_completion_unproven", "SoVITS 日志没有证明第 8 轮和 training done", {"log": str(log_path)})
        final_paths = [
            Path(lightweight[8][1]["path"]),
            Path(generators[lightweight[8][0]]["path"]),
            Path(discriminators[lightweight[8][0]]["path"]),
        ]
        for path in final_paths:
            structure = _pytorch_zip_structure(path)
            metadata = inspector(path)
            if metadata.get("format") != "pytorch-zip" or not metadata.get("top_level_keys"):
                raise CLIError("checkpoint_metadata_invalid", "SoVITS 最终检查点元数据无效", {"path": str(path)})
            if _sha256(path) != next(item["sha256"] for item in checkpoints if item["path"] == str(path)):
                raise CLIError("checkpoint_replaced", "SoVITS 最终检查点在验证期间变化", {"path": str(path)})
    else:
        lightweight = {}
        for item in by_kind.get("lightweight", []):
            match = re.fullmatch(rf"{re.escape(speaker)}-e(\d+)\.ckpt", Path(item["path"]).name)
            if match:
                lightweight[int(match.group(1))] = item
        full = {}
        for item in by_kind.get("full_training", []):
            match = re.fullmatch(r"epoch=(\d+)-step=(\d+)\.ckpt", Path(item["path"]).name)
            if match:
                full[int(match.group(1))] = (int(match.group(2)), item)
        expected_light = set(range(1, 11))
        expected_full = set(range(10))
        if (
            len(by_kind.get("lightweight", [])) != 10
            or len(by_kind.get("full_training", [])) != 10
            or set(lightweight) != expected_light
            or set(full) != expected_full
            or any(full[epoch][0] != (epoch + 1) * 21 for epoch in expected_full)
        ):
            raise CLIError("checkpoint_count_mismatch", "GPT 必须恰好完成总计 10 轮且轻量/完整检查点逐轮配对", {"log": str(log_path)})
        for item in checkpoints:
            _pytorch_zip_structure(Path(item["path"]))
        if not re.search(r"max_epochs=10`? reached", log_text):
            raise CLIError("training_completion_unproven", "GPT 日志没有证明总计 10 轮完成", {"log": str(log_path)})
        for path in (Path(lightweight[10]["path"]), Path(full[9][1]["path"])):
            _pytorch_zip_structure(path)
        final_full = Path(full[9][1]["path"])
        _validate_gpt_metadata(final_full, inspector(final_full), epoch=9, step=210)
        if _sha256(final_full) != full[9][1]["sha256"]:
            raise CLIError("checkpoint_replaced", "GPT 最终完整检查点在验证期间变化", {"path": str(final_full)})


def _controlled_gpt_resume_command(
    settings: Settings,
    runtime_dir: Path,
    script: Path,
    config: Path,
    resume_checkpoint: dict,
) -> list[str]:
    launcher = runtime_dir / "gpt_resume_launcher.py"
    _atomic_text(launcher, GPT_RESUME_LAUNCHER)
    return [
        str(settings.runtime),
        "-s",
        str(launcher),
        "--resume-path",
        resume_checkpoint["path"],
        "--resume-sha256",
        resume_checkpoint["sha256"],
        "--script",
        str(script),
        "--",
        "--config_file",
        str(config),
    ]


def run_preprocessing(
    settings: Settings,
    workspace_path: str | Path,
    *,
    dry_run: bool = False,
    process_runner: ProcessRunner | None = None,
    upstream_status_reader: Callable[[], list[str]] | None = None,
) -> dict:
    workspace, plan = _load_plan(workspace_path, settings=settings)
    expected_count = int(plan["expected_approved_count"])
    upstream_evidence: list[dict] = []
    marker = workspace / "preprocess.json"
    if marker.exists() and not dry_run:
        raise CLIError("preprocess_exists", "预处理已经执行，默认拒绝覆盖", {"path": str(marker)})
    features = workspace / "features"
    scripts = settings.checkout / "GPT_SoVITS" / "prepare_datasets"
    labels = workspace / "training.list"
    base_env = os.environ.copy()
    base_env.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "NO_PROXY": "*",
            "inp_text": str(labels),
            "inp_wav_dir": "",
            "exp_name": plan["speaker"],
            "opt_dir": str(features),
            "i_part": "0",
            "all_parts": "1",
            "_CUDA_VISIBLE_DEVICES": "0",
            "is_half": "True",
            "version": TARGET_VERSION,
            "bert_pretrained_dir": str(settings.checkout / "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large"),
            "cnhubert_base_dir": str(settings.checkout / "GPT_SoVITS/pretrained_models/chinese-hubert-base"),
            "sv_path": str(settings.checkout / "GPT_SoVITS/pretrained_models/sv/pretrained_eres2netv2w24s4ep4.ckpt"),
            "pretrained_s2G": str(settings.checkout / "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2ProPlus.pth"),
            "s2config_path": str(workspace / "configs/s2-preprocess.json"),
        }
    )
    steps = [
        ("text", scripts / "1-get-text.py"),
        ("hubert", scripts / "2-get-hubert-wav32k.py"),
        ("speaker_vector", scripts / "2-get-sv.py"),
        ("semantic", scripts / "3-get-semantic.py"),
    ]
    commands = [[str(settings.runtime), "-s", str(script)] for _, script in steps]
    resume_states = [_resume_state(features, name, expected_count) for name, _ in steps]
    if dry_run:
        return {
            "dry_run": True,
            "workspace": str(workspace),
            "steps": [
                {"name": name, "command": command, "resume": state[0]}
                for (name, _), command, state in zip(steps, commands, resume_states)
            ],
        }
    upstream_evidence = _assert_allowed_upstream_status(settings, upstream_status_reader)
    missing = [str(script) for _, script in steps if not script.is_file()]
    if missing:
        raise CLIError("preprocess_script_missing", "上游预处理脚本缺失", {"missing": missing})
    runner = process_runner or _default_runner
    results = []
    for (name, _), command, resume_state in zip(steps, commands, resume_states):
        if resume_state[0] == "verified":
            results.append({"step": name, "status": "skipped_verified", "count": resume_state[1], "log": None})
            continue
        log_path = _next_log_path(workspace, name)
        exit_code = runner(command, cwd=settings.checkout, env=base_env.copy(), log_path=log_path)
        if exit_code != 0:
            raise CLIError("preprocess_failed", f"预处理 {name} 退出码 {exit_code}", {"step": name, "exit_code": exit_code, "log": str(log_path)})
        if not log_path.is_file():
            log_path.write_text("runner completed without captured log\n", encoding="utf-8")
        if name == "text":
            part = features / "2-name2text-0.txt"
            count = _verify_count(part, expected_count, artifact=name)
            os.replace(part, features / "2-name2text.txt")
        elif name == "hubert":
            count = min(
                _verify_count(features / "4-cnhubert", expected_count, artifact="cnhubert", directory=True),
                _verify_count(features / "5-wav32k", expected_count, artifact="wav32k", directory=True),
            )
        elif name == "speaker_vector":
            count = _verify_count(features / "7-sv_cn", expected_count, artifact=name, directory=True)
        else:
            part = features / "6-name2semantic-0.tsv"
            count = _verify_count(part, expected_count, artifact=name)
            lines = [line for line in part.read_text(encoding="utf-8").splitlines() if line.strip()]
            (features / "6-name2semantic.tsv").write_text("item_name\tsemantic_audio\n" + "\n".join(lines) + "\n", encoding="utf-8")
            part.unlink()
        results.append({"step": name, "status": "completed", "count": count, "log": str(log_path)})
        _load_plan(workspace, settings=settings)
        upstream_evidence = _assert_allowed_upstream_status(settings, upstream_status_reader)
    evidence = {
        "status": "completed",
        "completed_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "manifest_sha256": plan["manifest"]["sha256"],
        "allowed_upstream_caches": upstream_evidence,
        "steps": results,
    }
    _atomic_json(marker, evidence)
    return {"dry_run": False, "workspace": str(workspace), **evidence}


def run_trial_training(
    settings: Settings,
    workspace_path: str | Path,
    target: str,
    *,
    dry_run: bool = False,
    process_runner: ProcessRunner | None = None,
    upstream_status_reader: Callable[[], list[str]] | None = None,
    checkpoint_inspector: CheckpointInspector | None = None,
) -> dict:
    workspace, plan = _load_plan(workspace_path, settings=settings)
    if target not in {"sovits", "gpt"}:
        raise CLIError("invalid_training_target", "训练目标只能是 sovits 或 gpt")
    parameters = plan["parameters"][target]
    config = workspace / "configs" / ("s2.json" if target == "sovits" else "s1.yaml")
    script = settings.checkout / "GPT_SoVITS" / ("s2_train.py" if target == "sovits" else "s1_train.py")
    command = [str(settings.runtime), "-s", str(script), "--config" if target == "sovits" else "--config_file", str(config)]
    if dry_run:
        return {"dry_run": True, "workspace": str(workspace), "target": target, "parameters": parameters, "command": command}
    preprocess = workspace / "preprocess.json"
    if not preprocess.is_file() or json.loads(preprocess.read_text(encoding="utf-8")).get("status") != "completed":
        raise CLIError("preprocess_required", "必须先完成全部预处理再开始训练")
    upstream_evidence = _assert_allowed_upstream_status(settings, upstream_status_reader)
    marker = workspace / f"train-{target}.json"
    if marker.exists():
        raise CLIError("training_exists", f"{target} 试训练已经执行，默认拒绝重复")
    log_path = _next_training_log_path(workspace, target)
    runtime_dir = workspace / "runtime-cwd" / target
    runtime_dir.mkdir(parents=True, exist_ok=True)
    if target == "sovits":
        (workspace / "features" / "logs_s2_v2ProPlus").mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "NO_PROXY": "*",
            "_CUDA_VISIBLE_DEVICES": "0",
            "hz": "25hz",
            "version": TARGET_VERSION,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    env.pop("TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD", None)
    env.pop("TORCH_FORCE_WEIGHTS_ONLY_LOAD", None)
    inspector = checkpoint_inspector or _runtime_checkpoint_inspector(settings)
    resume_checkpoint = _gpt_resume_checkpoint(workspace, plan, inspector) if target == "gpt" else None
    if resume_checkpoint is not None:
        # Keep PyTorch's weights_only safety default for every load. The controlled
        # launcher permits WindowsPath only while torch.load reads this exact,
        # hash-bound resume checkpoint; dataset/features loads remain untouched.
        command = _controlled_gpt_resume_command(settings, runtime_dir, script, config, resume_checkpoint)
    runner = process_runner or _default_runner
    if resume_checkpoint is not None and (
        Path(resume_checkpoint["path"]).resolve(strict=True)
        != (workspace / "internal/gpt/ckpt" / resume_checkpoint["name"]).resolve(strict=True)
        or _sha256(Path(resume_checkpoint["path"])) != resume_checkpoint["sha256"]
    ):
        raise CLIError("checkpoint_replaced", "GPT 完整检查点在 runner 启动前被替换或路径逃逸")
    exit_code = runner(command, cwd=runtime_dir, env=env, log_path=log_path)
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    danger = next((token for token in ("CUDA out of memory", "OutOfMemoryError", "nan", "NaN") if token in log_text), None)
    if exit_code != 0 or danger:
        if target == "gpt":
            _record_gpt_resume_trust(workspace, plan, inspector, exit_code=exit_code)
        raise CLIError(
            "training_failed",
            f"{target} 试训练失败，退出码 {exit_code}",
            {"target": target, "exit_code": exit_code, "stop_reason": danger, "log": str(log_path)},
        )
    if resume_checkpoint is not None and not re.search(
        rf"^ckpt_path:\s*.*{re.escape(resume_checkpoint['name'])}\s*$",
        log_text,
        flags=re.MULTILINE,
    ):
        raise CLIError(
            "gpt_resume_unproven",
            "GPT 训练结束但日志未证明从预期完整检查点恢复",
            {"checkpoint": resume_checkpoint, "log": str(log_path)},
        )
    if resume_checkpoint is not None and "Restored all states from the checkpoint" not in log_text:
        raise CLIError(
            "gpt_resume_unproven",
            "GPT 训练日志缺少 Restored all states，无法证明完整恢复",
            {"checkpoint": resume_checkpoint, "log": str(log_path)},
        )
    _load_plan(workspace, settings=settings)
    upstream_evidence = _assert_allowed_upstream_status(settings, upstream_status_reader)
    checkpoints = _checkpoint_records(workspace, target)
    _validate_training_checkpoints(target, checkpoints, log_path, inspector, plan["speaker"])
    evidence = {
        "status": "completed",
        "target": target,
        "completed_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "parameters": parameters,
        "log": str(log_path),
        "checkpoints": checkpoints,
        "allowed_upstream_caches": upstream_evidence,
    }
    if resume_checkpoint is not None:
        evidence["resume_checkpoint"] = resume_checkpoint
    _atomic_json(marker, evidence)
    return {"dry_run": False, "workspace": str(workspace), **evidence}


def training_workspace_status(workspace_path: str | Path) -> dict:
    workspace, plan = _load_plan(workspace_path, allow_legacy=True)
    checkpoints = []
    for target in ("sovits", "gpt"):
        checkpoints.extend(_checkpoint_records(workspace, target))
    stages = {}
    for name, relative in (("preprocess", "preprocess.json"), ("sovits", "train-sovits.json"), ("gpt", "train-gpt.json")):
        path = workspace / relative
        stages[name] = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"status": "not_started"}
    return {
        "workspace": str(workspace),
        "target": plan["target"],
        "approved_count": plan["manifest"]["approved_count"],
        "stages": stages,
        "checkpoints": checkpoints,
    }
