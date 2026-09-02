from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import wave
from pathlib import Path

from cli_anything.gpt_sovits.core.errors import CLIError


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pcm_rms(raw: bytes, sample_width: int) -> int:
    if not raw:
        return 0
    if sample_width not in {1, 2, 3, 4}:
        raise CLIError("unsupported_pcm", "不支持的 PCM 采样位宽", {"sample_width_bytes": sample_width})
    total = 0
    count = len(raw) // sample_width
    for offset in range(0, count * sample_width, sample_width):
        chunk = raw[offset : offset + sample_width]
        if sample_width == 1:
            value = chunk[0] - 128
        else:
            value = int.from_bytes(chunk, "little", signed=True)
        total += value * value
    return int(math.sqrt(total / count)) if count else 0


def inspect_wav(path: str | Path, require_non_silent: bool = False) -> dict:
    audio_path = Path(path).resolve()
    if not audio_path.is_file():
        raise CLIError("audio_not_found", "找不到音频文件", {"path": str(audio_path)})
    try:
        with audio_path.open("rb") as stream:
            magic = stream.read(12)
        if len(magic) < 12 or magic[:4] != b"RIFF" or magic[8:12] != b"WAVE":
            raise CLIError("invalid_wav", "文件不是 RIFF/WAVE 音频", {"path": str(audio_path)})
        with wave.open(str(audio_path), "rb") as wav:
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            sample_rate = wav.getframerate()
            frames = wav.getnframes()
            raw = wav.readframes(frames)
    except (wave.Error, EOFError) as exc:
        raise CLIError("invalid_wav", "WAV 文件损坏或不完整", {"path": str(audio_path), "reason": str(exc)}) from exc
    if sample_rate <= 0 or channels <= 0 or frames <= 0:
        raise CLIError("invalid_wav", "WAV 缺少有效的采样信息", {"path": str(audio_path)})
    rms = pcm_rms(raw, sample_width)
    if require_non_silent and rms <= 10:
        raise CLIError("silent_audio", "生成的音频接近静音", {"rms": rms})
    result = {
        "path": str(audio_path),
        "size_bytes": audio_path.stat().st_size,
        "duration_seconds": round(frames / sample_rate, 6),
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width_bytes": sample_width,
        "frames": frames,
        "rms": rms,
        "sha256": sha256_file(audio_path),
        "format": "wav",
    }
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=format_name,duration", "-of", "json", str(audio_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            raise CLIError("ffprobe_failed", "ffprobe 无法读取生成音频", {"stderr": proc.stderr.strip()})
        result["ffprobe"] = json.loads(proc.stdout)
    return result
