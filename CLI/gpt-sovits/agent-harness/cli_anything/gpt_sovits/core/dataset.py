from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
import uuid
import wave
from collections import OrderedDict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from cli_anything.gpt_sovits.core.audio import inspect_wav
from cli_anything.gpt_sovits.core.errors import CLIError
from cli_anything.gpt_sovits.core.paths import require_local_path


MANIFEST_FIELDS = (
    "audio_path",
    "source_path",
    "start",
    "end",
    "duration_seconds",
    "sha256",
    "processing",
    "text_ja",
    "asr_source",
    "review_status",
)
REVIEW_STATUSES = {"pending", "rejected", "approved"}
PROCESSING_METHODS = {"original", "uvr5"}


def parse_timecode(value: str | int | float | Decimal) -> float:
    """Parse seconds, MM:SS.mmm, or HH:MM:SS.mmm without truncation."""
    raw = str(value).strip()
    if not raw:
        raise CLIError("invalid_timecode", "时间码不能为空", {"value": raw})
    try:
        parts = raw.split(":")
        if len(parts) == 1:
            seconds = Decimal(parts[0])
        elif len(parts) == 2:
            minutes = int(parts[0])
            tail = Decimal(parts[1])
            if minutes < 0 or not Decimal(0) <= tail < Decimal(60):
                raise ValueError
            seconds = Decimal(minutes * 60) + tail
        elif len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            tail = Decimal(parts[2])
            if hours < 0 or not 0 <= minutes < 60 or not Decimal(0) <= tail < Decimal(60):
                raise ValueError
            seconds = Decimal(hours * 3600 + minutes * 60) + tail
        else:
            raise ValueError
        if not seconds.is_finite() or seconds < 0:
            raise ValueError
        return float(seconds)
    except (InvalidOperation, ValueError, TypeError):
        raise CLIError("invalid_timecode", "时间码格式应为秒数、MM:SS.mmm 或 HH:MM:SS.mmm", {"value": raw}) from None


def format_timecode(seconds: float) -> str:
    total_ms = round(float(seconds) * 1000)
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{millis:03d}"


def validate_interval(start: float, end: float, media_duration: float, minimum: float = 2.0, maximum: float = 10.0) -> dict:
    start_value = float(start)
    end_value = float(end)
    duration = end_value - start_value
    if start_value < 0 or end_value <= start_value:
        raise CLIError("invalid_interval", "终点必须晚于非负起点", {"start": start_value, "end": end_value})
    if end_value > float(media_duration) + 0.001:
        raise CLIError("media_boundary", "片段终点超过媒体时长", {"end": end_value, "media_duration": media_duration})
    if duration < minimum - 0.001:
        raise CLIError("clip_too_short", "训练候选片段不得短于 2 秒", {"duration_seconds": duration})
    if duration > maximum + 0.001:
        raise CLIError("clip_too_long", "训练候选片段不得长于 10 秒", {"duration_seconds": duration})
    return {"start_seconds": start_value, "end_seconds": end_value, "duration_seconds": round(duration, 6)}


def media_duration(path: str | Path) -> float:
    source = require_local_path(path, purpose="source")
    if not source.is_file():
        raise CLIError("media_not_found", "找不到本机媒体文件", {"path": str(source)})
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise CLIError("ffprobe_missing", "找不到 ffprobe；请先安装 FFmpeg")
    process = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(source)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if process.returncode != 0:
        raise CLIError("media_probe_failed", "ffprobe 无法读取媒体时长", {"path": str(source), "stderr": process.stderr.strip()})
    try:
        value = float(json.loads(process.stdout)["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CLIError("media_probe_failed", "ffprobe 没有返回有效媒体时长", {"path": str(source)}) from exc
    if not math.isfinite(value) or value <= 0:
        raise CLIError("media_probe_failed", "媒体时长无效", {"path": str(source), "duration": value})
    return value


def _commit_no_overwrite(temp_path: Path, output_path: Path, overwrite: bool) -> None:
    if overwrite:
        os.replace(temp_path, output_path)
        return
    try:
        os.link(temp_path, output_path)
    except FileExistsError as exc:
        raise CLIError("output_exists", "输出文件已存在；如需覆盖请加 --overwrite", {"path": str(output_path)}) from exc
    finally:
        temp_path.unlink(missing_ok=True)


def extract_clip(
    source: str | Path,
    start: str | float,
    end: str | float,
    output: str | Path,
    overwrite: bool = False,
    dry_run: bool = False,
) -> dict:
    source_path = require_local_path(source, purpose="source")
    output_path = require_local_path(output, purpose="output")
    if not source_path.is_file():
        raise CLIError("media_not_found", "找不到本机媒体文件", {"path": str(source_path)})
    if output_path.suffix.lower() != ".wav":
        raise CLIError("unsupported_output", "训练候选输出必须是 .wav", {"path": str(output_path)})
    if output_path.exists() and not overwrite:
        raise CLIError("output_exists", "输出文件已存在；如需覆盖请加 --overwrite", {"path": str(output_path)})
    start_seconds = parse_timecode(start)
    end_seconds = parse_timecode(end)
    interval = validate_interval(start_seconds, end_seconds, media_duration(source_path))
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise CLIError("ffmpeg_missing", "找不到 ffmpeg；请先安装 FFmpeg")
    temp_path = output_path.parent / f".{output_path.name}.{uuid.uuid4().hex}.tmp.wav"
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start_seconds:.6f}",
        "-t",
        f"{interval['duration_seconds']:.6f}",
        "-i",
        str(source_path),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "32000",
        "-c:a",
        "pcm_s16le",
        "-y",
        str(temp_path),
    ]
    plan = {
        "action": "dataset.extract",
        "source": str(source_path),
        "output": str(output_path),
        "start": format_timecode(start_seconds),
        "end": format_timecode(end_seconds),
        **interval,
        "command": command,
    }
    if dry_run:
        return {"dry_run": True, **plan}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
        if process.returncode != 0:
            raise CLIError("ffmpeg_failed", "ffmpeg 提取候选片段失败", {"exit_code": process.returncode, "stderr": process.stderr.strip()})
        inspection = inspect_training_wav(temp_path)
        if not inspection["compliant"]:
            raise CLIError("invalid_extracted_audio", "提取结果不符合训练 WAV 规范", {"issues": inspection["issues"]})
        _commit_no_overwrite(temp_path, output_path, overwrite)
        inspection = inspect_training_wav(output_path)
        return {**plan, "inspection": inspection}
    finally:
        temp_path.unlink(missing_ok=True)


def inspect_training_wav(path: str | Path) -> dict:
    audio_path = require_local_path(path, purpose="audio")
    report = inspect_wav(audio_path)
    audio_path = Path(report["path"])
    with wave.open(str(audio_path), "rb") as stream:
        raw = stream.readframes(stream.getnframes())
    width = int(report["sample_width_bytes"])
    peak_limit = (1 << (8 * width - 1)) - 1 if width > 1 else 127
    clipped = 0
    samples = len(raw) // width
    for offset in range(0, samples * width, width):
        chunk = raw[offset : offset + width]
        value = (chunk[0] - 128) if width == 1 else int.from_bytes(chunk, "little", signed=True)
        if abs(value) >= peak_limit * 0.99:
            clipped += 1
    clipping_fraction = round(clipped / samples, 8) if samples else 0.0
    issues: list[str] = []
    if report["sample_rate"] != 32000:
        issues.append("sample_rate")
    if report["channels"] != 1:
        issues.append("channels")
    if report["sample_width_bytes"] != 2:
        issues.append("sample_width")
    if report["duration_seconds"] < 2:
        issues.append("too_short")
    if report["duration_seconds"] > 10:
        issues.append("too_long")
    if report["rms"] <= 10:
        issues.append("silent")
    if clipping_fraction > 0.001:
        issues.append("clipping")
    return {**report, "clipping_fraction": clipping_fraction, "issues": issues, "compliant": not issues}


def find_duplicate_hashes(reports: list[dict]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for report in reports:
        grouped.setdefault(str(report["sha256"]), []).append(str(report["path"]))
    return {digest: paths for digest, paths in grouped.items() if len(paths) > 1}


def _valid_review_audit(record: dict) -> bool:
    reviewer = str(record.get("reviewed_by", "")).strip()
    raw_time = str(record.get("reviewed_at", "")).strip()
    if not reviewer or not raw_time:
        return False
    try:
        parsed = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _normalized_record(record: dict, source_durations: dict[Path, float]) -> OrderedDict:
    missing = [field for field in MANIFEST_FIELDS if field not in record]
    if missing:
        raise CLIError("invalid_manifest_record", "清单记录缺少必填字段", {"missing": missing})
    if not str(record["text_ja"]).strip():
        raise CLIError("invalid_manifest_record", "日语文本不能为空", {"field": "text_ja"})
    if record["processing"] not in PROCESSING_METHODS:
        raise CLIError("invalid_manifest_record", "处理方式必须是 original 或 uvr5", {"processing": record["processing"]})
    if record["review_status"] not in REVIEW_STATUSES:
        raise CLIError("invalid_manifest_record", "审核状态必须是 pending、rejected 或 approved", {"review_status": record["review_status"]})
    if record["review_status"] == "approved" and not _valid_review_audit(record):
        raise CLIError("manual_approval_required", "approved 必须带人工审核者和审核时间；程序不会自动批准")
    start = parse_timecode(record["start"])
    end = parse_timecode(record["end"])
    try:
        recorded_duration = float(record["duration_seconds"])
    except (TypeError, ValueError):
        raise CLIError("invalid_manifest_record", "清单时长必须是数字") from None
    if not math.isfinite(recorded_duration) or end <= start or abs((end - start) - recorded_duration) > 0.05:
        raise CLIError("invalid_manifest_record", "清单时间码与时长不一致")
    source_path = require_local_path(record["source_path"], purpose="source")
    if not source_path.is_file():
        raise CLIError("source_media_not_found", "清单来源媒体不存在或不是文件", {"source_path": str(source_path)})
    if source_path not in source_durations:
        source_durations[source_path] = media_duration(source_path)
    validate_interval(start, end, source_durations[source_path])
    audio_path = require_local_path(record["audio_path"], purpose="audio")
    if not audio_path.is_file():
        raise CLIError("invalid_manifest_record", "清单音频不存在", {"audio_path": str(audio_path)})
    actual = inspect_training_wav(audio_path)
    if not actual["compliant"]:
        raise CLIError(
            "noncompliant_manifest_audio",
            "清单音频不符合训练 WAV 规范",
            {"audio_path": str(audio_path), "issues": actual["issues"]},
        )
    if abs(float(actual["duration_seconds"]) - recorded_duration) > 0.05:
        raise CLIError(
            "manifest_duration_mismatch",
            "记录时长、时间码与实际 WAV 时长不一致",
            {
                "audio_path": str(audio_path),
                "recorded_duration": recorded_duration,
                "actual_duration": actual["duration_seconds"],
            },
        )
    if actual["sha256"] != record["sha256"]:
        raise CLIError("invalid_manifest_record", "清单 SHA-256 与音频不一致", {"audio_path": str(audio_path)})
    normalized = OrderedDict((field, record[field]) for field in MANIFEST_FIELDS)
    normalized["audio_path"] = str(audio_path)
    normalized["source_path"] = str(source_path)
    for optional in ("reviewed_by", "reviewed_at", "notes", "original_audio_path"):
        if optional in record:
            if optional == "original_audio_path":
                normalized[optional] = str(require_local_path(record[optional], purpose="audio"))
                continue
            normalized[optional] = record[optional]
    return normalized


def build_manifest(records: list[dict], output: str | Path, overwrite: bool = False, dry_run: bool = False) -> dict:
    if not records:
        raise CLIError("empty_manifest", "清单至少需要一条记录")
    output_path = require_local_path(output, purpose="manifest")
    if output_path.suffix.lower() != ".jsonl":
        raise CLIError("unsupported_manifest", "清单输出必须是 .jsonl")
    if output_path.exists() and not overwrite:
        raise CLIError("output_exists", "清单已存在；如需覆盖请加 --overwrite", {"path": str(output_path)})
    source_durations: dict[Path, float] = {}
    normalized = [_normalized_record(record, source_durations) for record in records]
    duplicates = find_duplicate_hashes(
        [{"sha256": item["sha256"], "path": item["audio_path"]} for item in normalized]
    )
    if duplicates:
        raise CLIError("duplicate_manifest_audio", "清单包含重复音频内容", {"duplicates": duplicates})
    result = {
        "action": "dataset.manifest",
        "output": str(output_path),
        "count": len(normalized),
        "total_duration_seconds": round(sum(float(item["duration_seconds"]) for item in normalized), 6),
        "status_counts": {status: sum(item["review_status"] == status for item in normalized) for status in sorted(REVIEW_STATUSES)},
    }
    if dry_run:
        return {"dry_run": True, **result}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            for item in normalized:
                stream.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        _commit_no_overwrite(temp_path, output_path, overwrite)
    finally:
        temp_path.unlink(missing_ok=True)
    return result
