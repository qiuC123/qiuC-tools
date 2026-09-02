from __future__ import annotations

import hashlib
import html
import json
import os
import subprocess
import tempfile
import uuid
import wave
from fractions import Fraction
from pathlib import Path

from cli_anything.gpt_sovits.core.dataset import inspect_training_wav
from cli_anything.gpt_sovits.core.errors import CLIError
from cli_anything.gpt_sovits.core.paths import require_local_path


def _write_plan(action: str, output: str | Path, *, overwrite: bool = False, dry_run: bool = False) -> dict:
    output_path = require_local_path(output, purpose=f"{action}_output")
    if output_path.exists() and not overwrite:
        raise CLIError("output_exists", "输出已存在；如需覆盖请显式使用 --overwrite", {"path": str(output_path)})
    return {"action": f"dataset.{action}", "output": str(output_path), "overwrite": overwrite, "dry_run": dry_run}


def transcribe_plan(output: str | Path, *, overwrite: bool = False, dry_run: bool = False, **_kwargs) -> dict:
    return _write_plan("transcribe", output, overwrite=overwrite, dry_run=dry_run)


def prepare_plan(output: str | Path, *, overwrite: bool = False, dry_run: bool = False, **_kwargs) -> dict:
    return _write_plan("prepare", output, overwrite=overwrite, dry_run=dry_run)


def index_plan(output: str | Path, *, overwrite: bool = False, dry_run: bool = False, **_kwargs) -> dict:
    return _write_plan("index", output, overwrite=overwrite, dry_run=dry_run)


def proofread_index_plan(output: str | Path, *, overwrite: bool = False, dry_run: bool = False, **_kwargs) -> dict:
    return _write_plan("proofread_index", output, overwrite=overwrite, dry_run=dry_run)


def uvr_compare_plan(output: str | Path, *, overwrite: bool = False, dry_run: bool = False, **_kwargs) -> dict:
    return _write_plan("uvr_compare", output, overwrite=overwrite, dry_run=dry_run)


def _as_fraction(value) -> Fraction:
    return value if isinstance(value, Fraction) else Fraction(str(value))


def _floor_microseconds(value: Fraction) -> float:
    microseconds = value.numerator * 1_000_000 // value.denominator
    return microseconds / 1_000_000


def wav_frame_duration(path: str | Path) -> Fraction:
    audio_path = require_local_path(path, purpose="audio")
    if not audio_path.is_file():
        raise CLIError("audio_not_found", "找不到 ASR 音频", {"path": str(audio_path)})
    try:
        with wave.open(str(audio_path), "rb") as stream:
            frames = stream.getnframes()
            sample_rate = stream.getframerate()
    except (wave.Error, EOFError) as exc:
        raise CLIError("invalid_wav", "ASR 输入不是有效 WAV", {"path": str(audio_path)}) from exc
    if frames <= 0 or sample_rate <= 0:
        raise CLIError("invalid_wav", "ASR 输入缺少有效帧或采样率", {"path": str(audio_path)})
    return Fraction(frames, sample_rate)


def clamp_asr_segments(segments: list[dict], duration: float | Fraction) -> dict:
    exact_duration = _as_fraction(duration)
    if exact_duration <= 0:
        raise CLIError("invalid_audio_duration", "音频时长必须大于 0")
    corrected: list[dict] = []
    clamp_count = 0
    for source in segments:
        item = dict(source)
        try:
            original_start = _as_fraction(item["start"])
            original_end = _as_fraction(item["end"])
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            raise CLIError("invalid_asr_segment", "ASR 分段必须包含数字 start/end") from exc
        start = min(max(original_start, Fraction(0)), exact_duration)
        end = min(max(original_end, start), exact_duration)
        changed = start != original_start or end != original_end
        if start != original_start:
            item["original_start"] = float(original_start)
        if end != original_end:
            item["original_end"] = float(original_end)
        item["start"] = _floor_microseconds(start)
        item["end"] = _floor_microseconds(end)
        if changed:
            item["clamped"] = True
            clamp_count += 1
        corrected.append(item)
    return {"segments": corrected, "clamp_count": clamp_count}


def _read_json(path: str | Path, *, purpose: str) -> object:
    local = require_local_path(path, purpose=purpose)
    if not local.is_file():
        raise CLIError("input_not_found", "找不到流程输入文件", {"path": str(local)})
    try:
        return json.loads(local.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CLIError("invalid_workflow_input", "流程输入必须是 UTF-8 JSON", {"path": str(local)}) from exc


def _read_jsonl(path: str | Path, *, purpose: str) -> list[dict]:
    local = require_local_path(path, purpose=purpose)
    if not local.is_file():
        raise CLIError("input_not_found", "找不到流程输入文件", {"path": str(local)})
    try:
        return [json.loads(line) for line in local.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CLIError("invalid_workflow_input", "流程输入必须是 UTF-8 JSONL", {"path": str(local)}) from exc


def _atomic_bytes(output: Path, payload: bytes, overwrite: bool) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temp, output)
        else:
            try:
                os.link(temp, output)
            except FileExistsError as exc:
                raise CLIError("output_exists", "输出在提交时已存在", {"path": str(output)}) from exc
    finally:
        temp.unlink(missing_ok=True)


def transcribe_candidates(
    audio: list[str | Path],
    output: str | Path,
    *,
    runtime: str | Path,
    model_path: str | Path,
    overwrite: bool = False,
    dry_run: bool = False,
    runner=subprocess.run,
) -> dict:
    plan = transcribe_plan(output, overwrite=overwrite, dry_run=dry_run)
    output_path = Path(plan["output"])
    runtime_path = require_local_path(runtime, purpose="asr_runtime")
    model = require_local_path(model_path, purpose="asr_model")
    audio_paths = [require_local_path(item, purpose="audio") for item in audio]
    if not runtime_path.is_file() or not model.is_dir() or not audio_paths or not all(item.is_file() for item in audio_paths):
        raise CLIError("workflow_input_missing", "ASR runtime、模型目录和音频必须全部存在")
    worker = Path(__file__).parents[1] / "utils" / "offline_asr_worker.py"
    command = [str(runtime_path), str(worker), "--model", str(model), *[str(item) for item in audio_paths]]
    if dry_run:
        return {**plan, "count": len(audio_paths), "command": command}
    environment = dict(os.environ)
    environment.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    process = runner(command, capture_output=True, text=True, encoding="utf-8", errors="replace", env=environment, timeout=3600)
    if process.returncode != 0:
        raise CLIError("offline_asr_failed", "离线 ASR 执行失败", {"exit_code": process.returncode})
    rows: list[dict] = []
    clamp_total = 0
    try:
        raw_rows = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
    except json.JSONDecodeError as exc:
        raise CLIError("invalid_asr_output", "离线 ASR worker 未返回有效 JSONL") from exc
    if len(raw_rows) != len(audio_paths):
        raise CLIError("invalid_asr_output", "离线 ASR 返回条数与音频数量不一致")
    for expected, row in zip(audio_paths, raw_rows):
        if require_local_path(row.get("audio_path", ""), purpose="audio") != expected:
            raise CLIError("invalid_asr_output", "离线 ASR 返回了意外音频路径")
        duration = wav_frame_duration(expected)
        fixed = clamp_asr_segments(row.get("segments", []), duration)
        row["segments"] = fixed["segments"]
        row["segment_clamp_count"] = fixed["clamp_count"]
        row["audio_duration_seconds"] = _floor_microseconds(duration)
        row["review_status"] = "pending"
        clamp_total += fixed["clamp_count"]
        rows.append(row)
    payload = "".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n" for item in rows).encode("utf-8")
    _atomic_bytes(output_path, payload, overwrite)
    return {**plan, "count": len(rows), "segment_clamp_count": clamp_total}


def prepare_records(
    plan_path: str | Path,
    drafts_path: str | Path,
    output: str | Path,
    inspection_output: str | Path,
    *,
    overwrite: bool = False,
    dry_run: bool = False,
) -> dict:
    plan_result = prepare_plan(output, overwrite=overwrite, dry_run=dry_run)
    output_path = Path(plan_result["output"])
    inspection_path = require_local_path(inspection_output, purpose="inspection_output")
    if inspection_path.exists() and not overwrite:
        raise CLIError("output_exists", "检查报告已存在；如需覆盖请显式使用 --overwrite", {"path": str(inspection_path)})
    plan = _read_json(plan_path, purpose="candidate_plan")
    drafts = {Path(row["audio_path"]).stem: row for row in _read_jsonl(drafts_path, purpose="asr_drafts")}
    if not isinstance(plan, dict) or not isinstance(plan.get("records"), list):
        raise CLIError("invalid_workflow_input", "候选计划必须包含 records 数组")
    root = require_local_path(plan_path, purpose="candidate_plan").parent
    records: list[dict] = []
    inspections: list[dict] = []
    for clip in plan["records"]:
        audio_path = require_local_path(root / "original" / f"{clip['id']}.wav", purpose="audio")
        inspection = inspect_training_wav(audio_path)
        if not inspection["compliant"]:
            raise CLIError("noncompliant_workflow_audio", "候选音频不符合训练规范", {"path": str(audio_path)})
        inspections.append(inspection)
        draft = drafts.get(clip["id"])
        if not draft or not str(draft.get("text_ja", "")).strip():
            raise CLIError("missing_asr_draft", "候选缺少非空 ASR 草稿", {"id": clip["id"]})
        records.append(
            {
                "audio_path": str(audio_path),
                "source_path": str(require_local_path(plan["source"], purpose="source")),
                "start": clip["start"],
                "end": clip["end"],
                "duration_seconds": inspection["duration_seconds"],
                "sha256": inspection["sha256"],
                "processing": "original",
                "text_ja": draft["text_ja"],
                "asr_source": draft.get("asr_source", "offline-asr-pending"),
                "review_status": "pending",
                "notes": f"场景：{clip.get('scene', '')}；ASR 仅为待人工核对草稿。",
            }
        )
    if dry_run:
        return {**plan_result, "inspection_output": str(inspection_path), "count": len(records)}
    # Both payloads are fully built and validated before either destination is changed.
    records_payload = (json.dumps(records, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    inspection_payload = (json.dumps(inspections, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _atomic_bytes(output_path, records_payload, overwrite)
    try:
        _atomic_bytes(inspection_path, inspection_payload, overwrite)
    except Exception:
        if not overwrite:
            output_path.unlink(missing_ok=True)
        raise
    return {**plan_result, "inspection_output": str(inspection_path), "count": len(records)}


def build_listening_index(
    manifest: str | Path,
    output: str | Path,
    *,
    comparison_dir: str | Path | None = None,
    overwrite: bool = False,
    dry_run: bool = False,
) -> dict:
    plan = index_plan(output, overwrite=overwrite, dry_run=dry_run)
    output_path = Path(plan["output"])
    rows = _read_jsonl(manifest, purpose="manifest")
    comparison = require_local_path(comparison_dir, purpose="comparison_dir") if comparison_dir else None
    status_counts = {"approved": 0, "pending": 0, "rejected": 0}
    for row in rows:
        status = str(row.get("review_status", ""))
        if status not in status_counts:
            raise CLIError("invalid_manifest_record", "试听索引遇到未知审核状态", {"review_status": status})
        status_counts[status] += 1
    total = sum(float(row["duration_seconds"]) for row in rows)
    cards = []
    for index, row in enumerate(rows, 1):
        audio_path = require_local_path(row["audio_path"], purpose="audio")
        relative_audio = os.path.relpath(audio_path, output_path.parent).replace("\\", "/")
        comparison_player = ""
        if comparison:
            candidate = comparison / f"{audio_path.stem}-uvr5-hp5.wav"
            if candidate.is_file():
                relative_comparison = os.path.relpath(candidate, output_path.parent).replace("\\", "/")
                comparison_player = f"<p><b>UVR5 HP5 对照版（仍为 pending）：</b></p><audio controls preload='none' src='{html.escape(relative_comparison)}'></audio>"
        status = str(row["review_status"])
        text_label = {"approved": "已确认日语", "pending": "离线 ASR 草稿", "rejected": "历史日语草稿"}[status]
        cards.append(
            "<article>"
            f"<h2>{index:02d}. {html.escape(audio_path.stem)}</h2>"
            f"<audio controls preload='none' src='{html.escape(relative_audio)}'></audio>{comparison_player}"
            f"<p><b>时间码：</b>{html.escape(str(row['start']))} → {html.escape(str(row['end']))}　<b>时长：</b>{float(row['duration_seconds']):.3f}s　<b>状态：</b>{html.escape(str(row['review_status']))}</p>"
            f"<p><b>{text_label}：</b>{html.escape(str(row['text_ja']))}</p>"
            f"<p><b>SHA-256：</b><code>{html.escape(str(row['sha256']))}</code></p></article>"
        )
    if status_counts["pending"]:
        notice = f"仍有 {status_counts['pending']} 条 pending 记录需要人工核对；程序不会自动标记 approved。"
    else:
        notice = f"日语文本校对已收口：approved {status_counts['approved']} 条，rejected {status_counts['rejected']} 条。"
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>训练候选试听索引</title>
<style>body{{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem;background:#f7f7f5;color:#222}}article{{background:white;border:1px solid #ddd;border-radius:12px;padding:1rem;margin:1rem 0}}audio{{width:100%}}code{{font-size:.78rem;word-break:break-all}}.notice{{border-left:5px solid #b86b00;padding:.8rem;background:#fff3dd}}</style></head>
<body><h1>训练候选试听索引</h1><p class="notice"><b>审核状态：</b>{notice}</p>
<p>总记录：{len(rows)} 条；approved {status_counts['approved']} / pending {status_counts['pending']} / rejected {status_counts['rejected']}。总时长：{total:.3f} 秒（{total / 60:.2f} 分钟）。</p>{''.join(cards)}</body></html>
"""
    if dry_run:
        return {**plan, "count": len(rows), "total_duration_seconds": round(total, 6), "status_counts": status_counts}
    _atomic_bytes(output_path, document.encode("utf-8"), overwrite)
    return {**plan, "count": len(rows), "total_duration_seconds": round(total, 6), "status_counts": status_counts}


def build_proofreading_index(
    manifest: str | Path,
    proposals: str | Path,
    output: str | Path,
    *,
    overwrite: bool = False,
    dry_run: bool = False,
) -> dict:
    """Build a read-only human proofreading page without mutating manifest text."""
    plan = proofread_index_plan(output, overwrite=overwrite, dry_run=dry_run)
    output_path = Path(plan["output"])
    rows = _read_jsonl(manifest, purpose="manifest")
    proposal_data = _read_json(proposals, purpose="proofreading_proposals")
    if (
        not isinstance(proposal_data, dict)
        or proposal_data.get("version") != 1
        or not isinstance(proposal_data.get("records"), list)
        or not proposal_data["records"]
    ):
        raise CLIError("invalid_proofreading_input", "校对建议必须是 version=1 且包含非空 records 数组")

    manifest_by_id: dict[str, dict] = {}
    for row in rows:
        audio_id = Path(str(row.get("audio_path", ""))).stem
        if not audio_id or audio_id in manifest_by_id:
            raise CLIError("invalid_proofreading_input", "manifest 中的音频编号为空或重复", {"id": audio_id})
        manifest_by_id[audio_id] = row

    confidence_counts = {"high": 0, "medium": 0, "listen": 0}
    confidence_labels = {"high": "较高", "medium": "中等", "listen": "需重点听辨"}
    seen: set[str] = set()
    cards: list[str] = []
    total = 0.0
    for index, proposal in enumerate(proposal_data["records"], 1):
        if not isinstance(proposal, dict):
            raise CLIError("invalid_proofreading_record", "每条校对建议必须是对象", {"index": index})
        audio_id = str(proposal.get("id", "")).strip()
        text_zh = str(proposal.get("text_zh", "")).strip()
        text_ja = str(proposal.get("text_ja_proposed", "")).strip()
        confidence = str(proposal.get("confidence", "")).strip()
        notes = str(proposal.get("notes", "")).strip()
        if not audio_id or not text_zh or not text_ja:
            raise CLIError(
                "invalid_proofreading_record",
                "校对建议的 id、text_zh 和 text_ja_proposed 均不能为空",
                {"index": index, "id": audio_id},
            )
        if confidence not in confidence_counts:
            raise CLIError(
                "invalid_proofreading_confidence",
                "confidence 只能是 high、medium 或 listen",
                {"index": index, "id": audio_id, "confidence": confidence},
            )
        if audio_id in seen:
            raise CLIError("duplicate_proofreading_id", "校对建议包含重复编号", {"id": audio_id})
        seen.add(audio_id)
        row = manifest_by_id.get(audio_id)
        if row is None:
            raise CLIError("unknown_proofreading_id", "校对建议编号不在 manifest 中", {"id": audio_id})
        if row.get("review_status") != "pending":
            raise CLIError(
                "proofreading_status_mismatch",
                "只允许为 pending 记录生成校对建议",
                {"id": audio_id, "review_status": row.get("review_status")},
            )
        audio_path = require_local_path(row["audio_path"], purpose="audio")
        if not audio_path.is_file():
            raise CLIError("audio_not_found", "校对页引用的音频不存在", {"id": audio_id, "path": str(audio_path)})
        relative_audio = os.path.relpath(audio_path, output_path.parent).replace("\\", "/")
        duration = float(row["duration_seconds"])
        total += duration
        confidence_counts[confidence] += 1
        note_html = f"<p><b>说明：</b>{html.escape(notes)}</p>" if notes else ""
        cards.append(
            "<article>"
            f"<h2>{index:02d}. {html.escape(audio_id)}</h2>"
            f"<audio controls preload='none' src='{html.escape(relative_audio)}'></audio>"
            f"<p><b>时间码：</b>{html.escape(str(row['start']))} → {html.escape(str(row['end']))}　"
            f"<b>时长：</b>{duration:.3f}s　<b>建议置信度：</b>{confidence_labels[confidence]}</p>"
            f"<p><b>中文字幕：</b>{html.escape(text_zh)}</p>"
            f"<p><b>原始 ASR 草稿：</b>{html.escape(str(row['text_ja']))}</p>"
            f"<p class='proposal'><b>建议日语：</b>{html.escape(text_ja)}</p>{note_html}</article>"
        )

    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>训练文本校对索引</title>
<style>body{{font-family:system-ui,sans-serif;max-width:980px;margin:2rem auto;padding:0 1rem;background:#f7f7f5;color:#222}}article{{background:white;border:1px solid #ddd;border-radius:12px;padding:1rem;margin:1rem 0}}audio{{width:100%}}.notice{{border-left:5px solid #b86b00;padding:.8rem;background:#fff3dd}}.proposal{{background:#eef7ff;border-left:4px solid #2878b5;padding:.7rem}}</style></head>
<body><h1>训练文本校对索引</h1><p class="notice"><b>人工确认前不会回写：</b>建议文本由本机 ASR、已有字幕和听写推断整理，不是官方台本。请逐条听原音后确认或修改。</p>
<p>待校对：{len(cards)} 条，累计 {total:.3f} 秒。较高 {confidence_counts['high']} 条；中等 {confidence_counts['medium']} 条；需重点听辨 {confidence_counts['listen']} 条。</p>{''.join(cards)}</body></html>
"""
    result = {
        **plan,
        "count": len(cards),
        "total_duration_seconds": round(total, 6),
        "confidence_counts": confidence_counts,
    }
    if dry_run:
        return result
    _atomic_bytes(output_path, document.encode("utf-8"), overwrite)
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def create_uvr_comparison(
    audio: str | Path,
    output: str | Path,
    report_output: str | Path,
    *,
    runtime: str | Path,
    checkout: str | Path,
    weight: str | Path,
    overwrite: bool = False,
    dry_run: bool = False,
    runner=subprocess.run,
) -> dict:
    plan = uvr_compare_plan(output, overwrite=overwrite, dry_run=dry_run)
    output_path = Path(plan["output"])
    report_path = require_local_path(report_output, purpose="uvr_report_output")
    if report_path.exists() and not overwrite:
        raise CLIError("output_exists", "UVR5 报告已存在；如需覆盖请显式使用 --overwrite", {"path": str(report_path)})
    audio_path = require_local_path(audio, purpose="audio")
    runtime_path = require_local_path(runtime, purpose="uvr_runtime")
    checkout_path = require_local_path(checkout, purpose="checkout")
    weight_path = require_local_path(weight, purpose="uvr_weight")
    if not audio_path.is_file() or not runtime_path.is_file() or not checkout_path.is_dir() or not weight_path.is_file():
        raise CLIError("workflow_input_missing", "UVR5 runtime、checkout、权重和音频必须全部存在")
    worker = Path(__file__).parents[1] / "utils" / "uvr_worker.py"
    command_summary = [str(runtime_path), str(worker), "--checkout", str(checkout_path), "--weight", str(weight_path), "--input", str(audio_path)]
    if dry_run:
        return {**plan, "report_output": str(report_path), "command": command_summary}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output_path.parent / f".{output_path.name}.{uuid.uuid4().hex}.tmp.wav"
    command = [*command_summary, "--output", str(temporary_output)]
    environment = dict(os.environ)
    environment.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    try:
        process = runner(command, capture_output=True, text=True, encoding="utf-8", errors="replace", env=environment, timeout=1800)
        if process.returncode != 0:
            raise CLIError("uvr_compare_failed", "UVR5 对照生成失败", {"exit_code": process.returncode})
        inspection = inspect_training_wav(temporary_output)
        if not inspection["compliant"]:
            raise CLIError("invalid_uvr_output", "UVR5 对照输出不符合训练 WAV 规范", {"issues": inspection["issues"]})
        report = {
            "model": weight_path.stem,
            "aggressiveness": 10,
            "device": "cuda",
            "input": str(audio_path),
            "input_sha256": _sha256(audio_path),
            "output": str(output_path),
            "output_sha256": _sha256(temporary_output),
            "weight": str(weight_path),
            "weight_sha256": _sha256(weight_path),
            "review_status": "pending",
            "note": "仅供与 original 并排试听；不得自动替代原声或标记 approved。",
        }
        report_payload = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        # Commit the validated WAV first; on a no-overwrite report race, remove only the new WAV.
        if overwrite:
            os.replace(temporary_output, output_path)
        else:
            try:
                os.link(temporary_output, output_path)
            except FileExistsError as exc:
                raise CLIError("output_exists", "UVR5 输出在提交时已存在", {"path": str(output_path)}) from exc
        try:
            _atomic_bytes(report_path, report_payload, overwrite)
        except Exception:
            if not overwrite:
                output_path.unlink(missing_ok=True)
            raise
        return {**plan, "report_output": str(report_path), "inspection": inspection, "report": report}
    finally:
        temporary_output.unlink(missing_ok=True)
