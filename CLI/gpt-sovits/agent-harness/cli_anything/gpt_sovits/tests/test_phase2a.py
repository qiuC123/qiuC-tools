from __future__ import annotations

import hashlib
import importlib
import io
import json
import stat
import subprocess
import zipfile
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from cli_anything.gpt_sovits.core.config import Settings
from cli_anything.gpt_sovits.core.errors import CLIError
from cli_anything.gpt_sovits.core.dataset import (
    build_manifest,
    extract_clip,
    find_duplicate_hashes,
    inspect_training_wav,
    parse_timecode,
    validate_interval,
)
from cli_anything.gpt_sovits.core.training import training_doctor
from cli_anything.gpt_sovits.core.uvr import APPROVED_UVR_URL, safe_extract_uvr_zip, validate_uvr_url
from cli_anything.gpt_sovits.core.workflow import build_listening_index, build_proofreading_index
from cli_anything.gpt_sovits.gpt_sovits_cli import cli
from cli_anything.gpt_sovits.tests.test_core import make_wav


class NetworkTouched(RuntimeError):
    """Sentinel proving a remote path reached filesystem resolution."""


def _record(audio: Path, **overrides) -> dict:
    source = audio.parent / "source.wav"
    if not source.exists():
        make_wav(source, seconds=10, rate=44100)
    values = {
        "audio_path": str(audio.resolve()),
        "source_path": str(source.resolve()),
        "start": "00:00:00.000",
        "end": "00:00:02.000",
        "duration_seconds": 2.0,
        "sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
        "processing": "original",
        "text_ja": "テストです",
        "asr_source": "faster-whisper-base-offline",
        "review_status": "pending",
    }
    values.update(overrides)
    return values


class TestTimecodeAndExtraction:
    @pytest.mark.parametrize(
        "value,expected",
        [("01:02:42.840", 3762.84), ("02:03.500", 123.5), ("7.25", 7.25)],
    )
    def test_parse_timecode(self, value, expected):
        assert parse_timecode(value) == pytest.approx(expected)

    @pytest.mark.parametrize("value", ["", "-1", "00:61:00", "abc", "1:2:3:4"])
    def test_invalid_timecode_is_rejected(self, value):
        with pytest.raises(CLIError) as raised:
            parse_timecode(value)
        assert raised.value.code == "invalid_timecode"

    @pytest.mark.parametrize(
        "start,end,duration,code",
        [(5, 5, 30, "invalid_interval"), (6, 5, 30, "invalid_interval"), (20, 31, 30, "media_boundary"), (0, 11, 30, "clip_too_long")],
    )
    def test_interval_boundaries(self, start, end, duration, code):
        with pytest.raises(CLIError) as raised:
            validate_interval(start, end, duration)
        assert raised.value.code == code

    def test_extract_dry_run_is_side_effect_free(self, tmp_path, monkeypatch):
        source = tmp_path / "source.wav"
        make_wav(source, seconds=3)
        output = tmp_path / "new directory" / "片段.wav"
        monkeypatch.setattr("cli_anything.gpt_sovits.core.dataset.media_duration", lambda value: 3.0)
        monkeypatch.setattr("cli_anything.gpt_sovits.core.dataset.subprocess.run", lambda *args, **kwargs: pytest.fail("dry-run called ffmpeg"))
        result = extract_clip(source, "00:00:00.500", "00:00:02.500", output, dry_run=True)
        assert result["dry_run"] is True
        assert result["command"][0].lower().endswith("ffmpeg.exe") or result["command"][0].lower().endswith("ffmpeg")
        assert not output.parent.exists()

    def test_real_ffmpeg_extracts_training_pcm(self, tmp_path):
        source = make_wav(tmp_path / "来源.wav", seconds=5, rate=44100)
        output = tmp_path / "候选 01.wav"
        result = extract_clip(source, "00:00:01.000", "00:00:03.000", output)
        assert result["inspection"]["compliant"] is True
        assert result["inspection"]["sample_rate"] == 32000
        assert result["inspection"]["channels"] == 1
        assert result["inspection"]["sample_width_bytes"] == 2
        assert result["inspection"]["duration_seconds"] == pytest.approx(2, abs=0.05)

    def test_extract_refuses_existing_output(self, tmp_path):
        source = make_wav(tmp_path / "source.wav", seconds=3)
        output = tmp_path / "existing.wav"
        output.write_bytes(b"sentinel")
        with pytest.raises(CLIError) as raised:
            extract_clip(source, 0, 2, output)
        assert raised.value.code == "output_exists"
        assert output.read_bytes() == b"sentinel"

    def test_extract_failure_removes_temporary_output(self, tmp_path, monkeypatch):
        source = make_wav(tmp_path / "source.wav", seconds=3)
        output = tmp_path / "failed.wav"
        monkeypatch.setattr("cli_anything.gpt_sovits.core.dataset.media_duration", lambda value: 3.0)
        monkeypatch.setattr("cli_anything.gpt_sovits.core.dataset.subprocess.run", lambda *args, **kwargs: SimpleNamespace(returncode=2, stdout="", stderr="synthetic failure"))
        with pytest.raises(CLIError) as raised:
            extract_clip(source, 0, 2, output)
        assert raised.value.code == "ffmpeg_failed"
        assert not output.exists()
        assert not list(tmp_path.glob(".failed.wav.*.tmp.wav"))


class TestDatasetInspection:
    def test_compliant_audio_report(self, tmp_path):
        report = inspect_training_wav(make_wav(tmp_path / "ok.wav", seconds=2, rate=32000))
        assert report["compliant"] is True
        assert report["issues"] == []
        assert report["clipping_fraction"] == 0

    def test_silence_is_reported(self, tmp_path):
        report = inspect_training_wav(make_wav(tmp_path / "silent.wav", seconds=2, rate=32000, amplitude=0))
        assert report["compliant"] is False
        assert "silent" in report["issues"]

    def test_clipping_is_reported(self, tmp_path):
        path = tmp_path / "clipped.wav"
        make_wav(path, seconds=2, rate=32000, amplitude=32767)
        report = inspect_training_wav(path)
        assert report["clipping_fraction"] > 0
        assert "clipping" in report["issues"]

    def test_wrong_training_format_is_reported(self, tmp_path):
        report = inspect_training_wav(make_wav(tmp_path / "wrong.wav", seconds=2, rate=44100))
        assert report["compliant"] is False
        assert "sample_rate" in report["issues"]

    def test_duplicate_hashes_are_grouped(self, tmp_path):
        first = make_wav(tmp_path / "first.wav", seconds=2, rate=32000)
        second = tmp_path / "second.wav"
        second.write_bytes(first.read_bytes())
        reports = [inspect_training_wav(first), inspect_training_wav(second)]
        duplicates = find_duplicate_hashes(reports)
        assert list(duplicates.values()) == [[str(first.resolve()), str(second.resolve())]]


class TestManifest:
    def test_manifest_writes_stable_jsonl_and_preserves_pending(self, tmp_path):
        audio = make_wav(tmp_path / "candidate.wav", seconds=2, rate=32000)
        output = tmp_path / "manifest.jsonl"
        result = build_manifest([_record(audio)], output)
        line = json.loads(output.read_text(encoding="utf-8"))
        assert result["count"] == 1
        assert line["review_status"] == "pending"
        assert list(line) == [
            "audio_path", "source_path", "start", "end", "duration_seconds", "sha256",
            "processing", "text_ja", "asr_source", "review_status",
        ]

    def test_manifest_rejects_missing_required_text(self, tmp_path):
        audio = make_wav(tmp_path / "candidate.wav", seconds=2, rate=32000)
        with pytest.raises(CLIError) as raised:
            build_manifest([_record(audio, text_ja="")], tmp_path / "manifest.jsonl")
        assert raised.value.code == "invalid_manifest_record"

    def test_manifest_rejects_unattested_approved_status(self, tmp_path):
        audio = make_wav(tmp_path / "candidate.wav", seconds=2, rate=32000)
        with pytest.raises(CLIError) as raised:
            build_manifest([_record(audio, review_status="approved")], tmp_path / "manifest.jsonl")
        assert raised.value.code == "manual_approval_required"

    def test_manifest_accepts_explicit_human_approval_metadata(self, tmp_path):
        audio = make_wav(tmp_path / "candidate.wav", seconds=2, rate=32000)
        record = _record(audio, review_status="approved", reviewed_by="user", reviewed_at="2026-09-01T12:00:00+08:00")
        output = tmp_path / "manifest.jsonl"
        build_manifest([record], output)
        saved = json.loads(output.read_text(encoding="utf-8"))
        assert saved["reviewed_by"] == "user"

    def test_manifest_dry_run_creates_nothing(self, tmp_path):
        audio = make_wav(tmp_path / "candidate.wav", seconds=2, rate=32000)
        output = tmp_path / "new" / "manifest.jsonl"
        result = build_manifest([_record(audio)], output, dry_run=True)
        assert result["dry_run"] is True
        assert not output.parent.exists()

    def test_manifest_rejects_record_duration_that_disagrees_with_actual_wav(self, tmp_path):
        audio = make_wav(tmp_path / "candidate.wav", seconds=3.7, rate=32000)
        record = _record(audio, end="00:00:08.000", duration_seconds=8.0)
        with pytest.raises(CLIError) as raised:
            build_manifest([record], tmp_path / "manifest.jsonl")
        assert raised.value.code == "manifest_duration_mismatch"

    def test_manifest_requires_existing_local_source_media(self, tmp_path):
        audio = make_wav(tmp_path / "candidate.wav", seconds=2, rate=32000)
        missing = tmp_path / "missing-source.wav"
        with pytest.raises(CLIError) as raised:
            build_manifest([_record(audio, source_path=str(missing))], tmp_path / "manifest.jsonl")
        assert raised.value.code == "source_media_not_found"

    def test_manifest_checks_real_source_media_boundary(self, tmp_path):
        audio = make_wav(tmp_path / "candidate.wav", seconds=2, rate=32000)
        source = make_wav(tmp_path / "short-source.wav", seconds=3, rate=44100)
        record = _record(audio, source_path=str(source), start="00:00:02.000", end="00:00:04.000")
        with pytest.raises(CLIError) as raised:
            build_manifest([record], tmp_path / "manifest.jsonl")
        assert raised.value.code == "media_boundary"

    def test_manifest_rejects_noncompliant_audio(self, tmp_path):
        audio = make_wav(tmp_path / "wrong-rate.wav", seconds=2, rate=44100)
        with pytest.raises(CLIError) as raised:
            build_manifest([_record(audio)], tmp_path / "manifest.jsonl")
        assert raised.value.code == "noncompliant_manifest_audio"

    def test_manifest_rejects_duplicate_audio_hashes(self, tmp_path):
        first = make_wav(tmp_path / "first.wav", seconds=2, rate=32000)
        second = tmp_path / "second.wav"
        second.write_bytes(first.read_bytes())
        with pytest.raises(CLIError) as raised:
            build_manifest([_record(first), _record(second)], tmp_path / "manifest.jsonl")
        assert raised.value.code == "duplicate_manifest_audio"

    @pytest.mark.parametrize(
        "reviewed_by,reviewed_at",
        [("   ", "2026-09-01T12:00:00+08:00"), ("user", "not-a-time"), ("user", "2026-09-01T12:00:00")],
    )
    def test_manifest_rejects_invalid_approval_audit(self, tmp_path, reviewed_by, reviewed_at):
        audio = make_wav(tmp_path / "candidate.wav", seconds=2, rate=32000)
        record = _record(audio, review_status="approved", reviewed_by=reviewed_by, reviewed_at=reviewed_at)
        with pytest.raises(CLIError) as raised:
            build_manifest([record], tmp_path / "manifest.jsonl")
        assert raised.value.code == "manual_approval_required"


class TestTrainingDoctor:
    REQUIRED_V2PROPLUS = (
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

    def _settings(self, tmp_path):
        checkout = tmp_path / "checkout"
        required = [
            "GPT_SoVITS/prepare_datasets/1-get-text.py",
            "GPT_SoVITS/prepare_datasets/2-get-hubert-wav32k.py",
            "GPT_SoVITS/prepare_datasets/2-get-sv.py",
            "GPT_SoVITS/prepare_datasets/3-get-semantic.py",
            "GPT_SoVITS/s1_train.py",
            "GPT_SoVITS/s2_train.py",
        ]
        for relative in required:
            path = checkout / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# test", encoding="utf-8")
        for relative in self.REQUIRED_V2PROPLUS:
            path = checkout / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic fixture; validated only by the injected test validator")
        sv_model = checkout / "GPT_SoVITS/pretrained_models/sv/pretrained_eres2netv2w24s4ep4.ckpt"
        sv_model.parent.mkdir(parents=True, exist_ok=True)
        sv_model.write_bytes(b"synthetic speaker-vector fixture")
        (checkout / "api_v2.py").write_text("# test", encoding="utf-8")
        config = checkout / "GPT_SoVITS/configs/tts_infer.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("custom: {}", encoding="utf-8")
        runtime = checkout / "python.exe"
        runtime.write_bytes(b"x")
        return Settings.discover(str(checkout), str(runtime), state_dir=str(tmp_path / "state"))

    @staticmethod
    def _fixture_asset_validator(relative, path):
        expected = {
            b"synthetic fixture; validated only by the injected test validator",
            b"synthetic speaker-vector fixture",
        }
        return (True, None) if path.read_bytes() in expected else (False, "unexpected_test_fixture")

    def _complete_asr(self, tmp_path):
        cache = tmp_path / "asr"
        for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt"):
            (cache / name).parent.mkdir(parents=True, exist_ok=True)
            (cache / name).write_bytes(b"x")
        return cache

    def test_doctor_reports_missing_uvr_without_claiming_ready(self, tmp_path, monkeypatch):
        settings = self._settings(tmp_path)
        monkeypatch.setattr("cli_anything.gpt_sovits.core.training.shutil.which", lambda name: str(tmp_path / f"{name}.exe"))
        monkeypatch.setattr("cli_anything.gpt_sovits.core.training._runtime_probe", lambda value: {"ok": True, "cuda": True, "gpu": "RTX"})
        result = training_doctor(
            settings,
            asr_cache=self._complete_asr(tmp_path),
            uvr_dir=tmp_path / "missing-uvr",
            asset_validator=self._fixture_asset_validator,
        )
        assert result["ready"] is True
        assert result["required_ready"] is True
        assert result["uvr5"]["status"] == "missing_optional"
        assert "uvr5_weights" in result["optional_missing"]
        assert "uvr5_weights" not in result["missing"]

    def test_doctor_reports_incomplete_offline_asr_cache(self, tmp_path, monkeypatch):
        settings = self._settings(tmp_path)
        monkeypatch.setattr("cli_anything.gpt_sovits.core.training.shutil.which", lambda name: str(tmp_path / f"{name}.exe"))
        monkeypatch.setattr("cli_anything.gpt_sovits.core.training._runtime_probe", lambda value: {"ok": True, "cuda": True, "gpu": "RTX"})
        uvr = tmp_path / "uvr"
        uvr.mkdir()
        (uvr / "model.pth").write_bytes(b"x")
        result = training_doctor(
            settings,
            asr_cache=tmp_path / "missing-asr",
            uvr_dir=uvr,
            asset_validator=self._fixture_asset_validator,
        )
        assert result["ready"] is False
        assert result["offline_asr"]["status"] == "missing_required"

    @pytest.mark.parametrize("missing_kind", ["script", "model"])
    def test_doctor_requires_v2proplus_speaker_vector_assets(self, tmp_path, monkeypatch, missing_kind):
        settings = self._settings(tmp_path)
        relative = (
            "GPT_SoVITS/prepare_datasets/2-get-sv.py"
            if missing_kind == "script"
            else "GPT_SoVITS/pretrained_models/sv/pretrained_eres2netv2w24s4ep4.ckpt"
        )
        (settings.checkout / relative).unlink()
        monkeypatch.setattr("cli_anything.gpt_sovits.core.training.shutil.which", lambda name: str(tmp_path / f"{name}.exe"))
        monkeypatch.setattr("cli_anything.gpt_sovits.core.training._runtime_probe", lambda value: {"ok": True, "cuda": True, "gpu": "RTX"})
        result = training_doctor(
            settings,
            asr_cache=self._complete_asr(tmp_path),
            uvr_dir=tmp_path / "missing-uvr",
            asset_validator=self._fixture_asset_validator,
        )
        assert result["ready"] is False
        assert result["required_ready"] is False
        assert result["speaker_vector"]["status"] == "missing_required"

    def test_doctor_rejects_corrupt_speaker_vector_model(self, tmp_path, monkeypatch):
        settings = self._settings(tmp_path)
        model = settings.checkout / "GPT_SoVITS/pretrained_models/sv/pretrained_eres2netv2w24s4ep4.ckpt"
        model.write_bytes(b"x")
        monkeypatch.setattr("cli_anything.gpt_sovits.core.training.shutil.which", lambda name: str(tmp_path / f"{name}.exe"))
        monkeypatch.setattr("cli_anything.gpt_sovits.core.training._runtime_probe", lambda value: {"ok": True, "cuda": True, "gpu": "RTX"})
        result = training_doctor(
            settings,
            asr_cache=self._complete_asr(tmp_path),
            uvr_dir=tmp_path / "missing-uvr",
            asset_validator=self._fixture_asset_validator,
        )
        assert result["ready"] is False
        assert result["speaker_vector"]["status"] == "invalid_required"
        assert result["speaker_vector"]["validation_error"]

    def test_doctor_rejects_nonzero_but_corrupt_v2proplus_assets(self, tmp_path, monkeypatch):
        settings = self._settings(tmp_path)
        monkeypatch.setattr("cli_anything.gpt_sovits.core.training.shutil.which", lambda name: str(tmp_path / f"{name}.exe"))
        monkeypatch.setattr("cli_anything.gpt_sovits.core.training._runtime_probe", lambda value: {"ok": True, "cuda": True, "gpu": "RTX"})
        result = training_doctor(settings, asr_cache=self._complete_asr(tmp_path), uvr_dir=tmp_path / "missing-uvr")
        assert result["ready"] is False
        assert result["required_ready"] is False
        assert set(result["v2proplus_assets"]["invalid"]) == set(self.REQUIRED_V2PROPLUS)
        assert all(not item["ready"] and item["validation_error"] for item in result["v2proplus_assets"]["items"])

    @pytest.mark.parametrize("relative", REQUIRED_V2PROPLUS)
    def test_doctor_rejects_each_one_byte_asset_with_specific_invalid_result(self, tmp_path, monkeypatch, relative):
        settings = self._settings(tmp_path)
        (settings.checkout / relative).write_bytes(b"x")
        training_module = importlib.import_module("cli_anything.gpt_sovits.core.training")

        def validator(candidate, path):
            if candidate == relative:
                return training_module._validate_training_asset(candidate, path)
            return self._fixture_asset_validator(candidate, path)

        monkeypatch.setattr("cli_anything.gpt_sovits.core.training.shutil.which", lambda name: str(tmp_path / f"{name}.exe"))
        monkeypatch.setattr("cli_anything.gpt_sovits.core.training._runtime_probe", lambda value: {"ok": True, "cuda": True, "gpu": "RTX"})
        result = training_doctor(
            settings,
            asr_cache=self._complete_asr(tmp_path),
            uvr_dir=tmp_path / "missing-uvr",
            asset_validator=validator,
        )
        assert result["ready"] is False
        assert result["v2proplus_assets"]["invalid"] == [relative]

    def test_weight_validator_rejects_large_sparse_random_file_without_loading_it(self, tmp_path):
        module = importlib.import_module("cli_anything.gpt_sovits.core.training")
        relative = "GPT_SoVITS/pretrained_models/s1v3.ckpt"
        weight = tmp_path / "random.ckpt"
        with weight.open("wb") as stream:
            stream.write(b"obviously-not-a-pytorch-archive")
            stream.seek(module.MINIMUM_WEIGHT_BYTES[relative])
            stream.write(b"!")
        valid, reason = module._validate_training_asset(relative, weight)
        assert valid is False
        assert reason == "invalid_pytorch_archive_magic"

    @pytest.mark.parametrize("surface", ["asr_cache", "uvr_dir", "data_dir"])
    def test_doctor_rejects_remote_mapped_training_paths_before_access(self, tmp_path, monkeypatch, surface):
        settings = self._settings(tmp_path)
        paths = importlib.import_module("cli_anything.gpt_sovits.core.paths")
        monkeypatch.setattr(paths, "_get_drive_type", lambda root: paths.DRIVE_REMOTE if str(root).upper().startswith("Z:") else 3)
        original_resolve = Path.resolve
        remote_resolve_calls = []

        def reject_remote_resolve(candidate, *args, **kwargs):
            if str(candidate).replace("/", "\\").lower().startswith("z:\\remote-"):
                remote_resolve_calls.append(str(candidate))
                raise NetworkTouched(str(candidate))
            return original_resolve(candidate, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", reject_remote_resolve)
        monkeypatch.setattr("cli_anything.gpt_sovits.core.training.shutil.which", lambda name: str(tmp_path / f"{name}.exe"))
        monkeypatch.setattr("cli_anything.gpt_sovits.core.training._runtime_probe", lambda value: {"ok": True, "cuda": True, "gpu": "RTX"})
        local_uvr = tmp_path / "uvr"
        local_uvr.mkdir()
        (local_uvr / "model.pth").write_bytes(b"optional fixture")
        kwargs = {"asr_cache": self._complete_asr(tmp_path), "uvr_dir": local_uvr, "data_dir": tmp_path}
        kwargs[surface] = rf"Z:\remote-{surface}"
        with pytest.raises(CLIError) as raised:
            training_doctor(settings, **kwargs)
        assert raised.value.code == "nonlocal_path"
        assert remote_resolve_calls == []

    @pytest.mark.parametrize("surface", ["asr_cache", "uvr_dir", "data_dir"])
    def test_doctor_rejects_unc_created_by_expanduser_before_resolve(self, tmp_path, monkeypatch, surface):
        settings = self._settings(tmp_path)
        original_resolve = Path.resolve
        resolve_calls = []

        def reject_unc_resolve(candidate, *args, **kwargs):
            if str(candidate).replace("/", "\\").startswith("\\\\"):
                resolve_calls.append(str(candidate))
                raise NetworkTouched(str(candidate))
            return original_resolve(candidate, *args, **kwargs)

        local_uvr = tmp_path / "uvr"
        local_uvr.mkdir()
        (local_uvr / "model.pth").write_bytes(b"optional fixture")
        kwargs = {"asr_cache": self._complete_asr(tmp_path), "uvr_dir": local_uvr, "data_dir": tmp_path}
        kwargs[surface] = r"~\asset.wav"
        monkeypatch.setenv("USERPROFILE", r"\\server\share")
        monkeypatch.setattr(Path, "resolve", reject_unc_resolve)
        with pytest.raises(CLIError) as raised:
            training_doctor(settings, **kwargs)
        assert raised.value.code == "nonlocal_path"
        assert resolve_calls == []

    @pytest.mark.parametrize("relative", REQUIRED_V2PROPLUS)
    @pytest.mark.parametrize("mode", ["missing", "zero", "directory"])
    def test_doctor_rejects_each_missing_or_fake_v2proplus_asset(self, tmp_path, monkeypatch, relative, mode):
        settings = self._settings(tmp_path)
        asset = settings.checkout / relative
        if mode == "missing":
            asset.unlink()
        elif mode == "zero":
            asset.write_bytes(b"")
        else:
            asset.unlink()
            asset.mkdir()
        monkeypatch.setattr("cli_anything.gpt_sovits.core.training.shutil.which", lambda name: str(tmp_path / f"{name}.exe"))
        monkeypatch.setattr("cli_anything.gpt_sovits.core.training._runtime_probe", lambda value: {"ok": True, "cuda": True, "gpu": "RTX"})
        uvr = tmp_path / "uvr"
        uvr.mkdir()
        (uvr / "model.pth").write_bytes(b"x")
        result = training_doctor(
            settings,
            asr_cache=self._complete_asr(tmp_path),
            uvr_dir=uvr,
            asset_validator=self._fixture_asset_validator,
        )
        assert result["ready"] is False
        assert result["required_ready"] is False
        assert result["v2proplus_assets"]["status"] in {"missing_required", "invalid_required"}
        bucket = "missing" if mode == "missing" else "invalid"
        assert relative in result["v2proplus_assets"][bucket]


class TestUVRSafety:
    def test_only_approved_uvr_url_is_allowed(self):
        assert validate_uvr_url(APPROVED_UVR_URL) == APPROVED_UVR_URL
        with pytest.raises(CLIError):
            validate_uvr_url("https://example.com/uvr5_weights.zip")

    def test_safe_zip_extracts_regular_files(self, tmp_path):
        archive = tmp_path / "uvr.zip"
        with zipfile.ZipFile(archive, "w") as stream:
            stream.writestr("HP2_all_vocals.pth", b"weights")
        output = tmp_path / "weights"
        result = safe_extract_uvr_zip(archive, output)
        assert result["files"] == ["HP2_all_vocals.pth"]
        assert (output / "HP2_all_vocals.pth").read_bytes() == b"weights"

    def test_official_archive_layout_strips_top_folder_and_preserves_gitignore(self, tmp_path):
        archive = tmp_path / "uvr.zip"
        with zipfile.ZipFile(archive, "w") as stream:
            stream.writestr("uvr5_weights/.gitignore", b"archive rule")
            stream.writestr("uvr5_weights/HP2_all_vocals.pth", b"weights")
        output = tmp_path / "uvr5_weights"
        output.mkdir()
        (output / ".gitignore").write_text("user rule", encoding="utf-8")
        result = safe_extract_uvr_zip(archive, output)
        assert result["files"] == ["HP2_all_vocals.pth"]
        assert (output / "HP2_all_vocals.pth").read_bytes() == b"weights"
        assert (output / ".gitignore").read_text(encoding="utf-8") == "user rule"

    @pytest.mark.parametrize("name", ["../escape.pth", "/absolute.pth", "C:/drive.pth"])
    def test_zip_rejects_unsafe_paths(self, tmp_path, name):
        archive = tmp_path / "unsafe.zip"
        with zipfile.ZipFile(archive, "w") as stream:
            stream.writestr(name, b"bad")
        with pytest.raises(CLIError) as raised:
            safe_extract_uvr_zip(archive, tmp_path / "weights")
        assert raised.value.code == "unsafe_archive"

    @pytest.mark.parametrize(
        "name",
        ["folder/file.pth:secret", "CON/file.pth", "folder/NUL.pth", "folder/file.pth.", "folder/file.pth "],
    )
    def test_zip_rejects_ntfs_aliases_and_devices(self, tmp_path, name):
        archive = tmp_path / "unsafe.zip"
        with zipfile.ZipFile(archive, "w") as stream:
            stream.writestr(name, b"bad")
        with pytest.raises(CLIError) as raised:
            safe_extract_uvr_zip(archive, tmp_path / "weights")
        assert raised.value.code == "unsafe_archive"

    @pytest.mark.parametrize("names", [("A.pth", "a.pth"), ("File.pth", "Ｆｉｌｅ.pth")])
    def test_zip_rejects_case_or_normalization_alias_conflicts(self, tmp_path, names):
        archive = tmp_path / "aliases.zip"
        with zipfile.ZipFile(archive, "w") as stream:
            for name in names:
                stream.writestr(name, name.encode("utf-8"))
        with pytest.raises(CLIError) as raised:
            safe_extract_uvr_zip(archive, tmp_path / "weights")
        assert raised.value.code == "unsafe_archive"

    def test_zip_mid_copy_failure_leaves_existing_output_unchanged(self, tmp_path, monkeypatch):
        archive = tmp_path / "two-files.zip"
        with zipfile.ZipFile(archive, "w") as stream:
            stream.writestr("one.pth", b"one")
            stream.writestr("two.pth", b"two")
        output = tmp_path / "weights"
        output.mkdir()
        sentinel = output / ".gitignore"
        sentinel.write_bytes(b"sentinel")
        module = importlib.import_module("cli_anything.gpt_sovits.core.uvr")
        original = getattr(module, "_copy_zip_member")
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic copy failure")
            return original(*args, **kwargs)

        monkeypatch.setattr(module, "_copy_zip_member", fail_second)
        with pytest.raises(OSError):
            safe_extract_uvr_zip(archive, output)
        assert sorted(path.name for path in output.iterdir()) == [".gitignore"]
        assert sentinel.read_bytes() == b"sentinel"


class TestLocalPathPolicy:
    @pytest.mark.parametrize("raw", [r"\\server\share\file.wav", r"\\?\UNC\server\share\file.wav", "//server/share/file.wav"])
    def test_unc_and_extended_unc_are_rejected_without_network_access(self, raw):
        module = importlib.import_module("cli_anything.gpt_sovits.core.paths")
        with pytest.raises(CLIError) as raised:
            module.require_local_path(raw, purpose="audio")
        assert raised.value.code == "nonlocal_path"

    def test_remote_mapped_drive_is_rejected_via_injected_drive_type(self, tmp_path, monkeypatch):
        module = importlib.import_module("cli_anything.gpt_sovits.core.paths")
        monkeypatch.setattr(module, "_get_drive_type", lambda _root: module.DRIVE_REMOTE)
        with pytest.raises(CLIError) as raised:
            module.require_local_path(tmp_path / "file.wav", purpose="audio")
        assert raised.value.code == "nonlocal_path"

    def test_all_dataset_path_surfaces_use_local_policy(self, tmp_path, monkeypatch):
        module = importlib.import_module("cli_anything.gpt_sovits.core.paths")
        calls = []

        def record_path(path, *, purpose):
            calls.append(purpose)
            return Path(path).resolve()

        monkeypatch.setattr("cli_anything.gpt_sovits.core.dataset.require_local_path", record_path)
        source = make_wav(tmp_path / "source.wav", seconds=4, rate=44100)
        output = tmp_path / "output.wav"
        extract_clip(source, 0, 2, output, dry_run=True)
        audio = make_wav(tmp_path / "audio.wav", seconds=2, rate=32000)
        inspect_training_wav(audio)
        build_manifest([_record(audio, source_path=str(source))], tmp_path / "manifest.jsonl", dry_run=True)
        assert {"source", "audio", "output", "manifest"}.issubset(calls)

    def test_records_input_rejects_remote_mapped_drive_before_file_access(self, tmp_path, monkeypatch):
        module = importlib.import_module("cli_anything.gpt_sovits.core.paths")
        monkeypatch.setattr(module, "_get_drive_type", lambda _root: module.DRIVE_REMOTE)
        cli_module = importlib.import_module("cli_anything.gpt_sovits.gpt_sovits_cli")
        with pytest.raises(CLIError) as raised:
            cli_module._load_records(str(tmp_path / "records.json"))
        assert raised.value.code == "nonlocal_path"


class TestResolveOrderLocalPathPolicy:
    @pytest.mark.parametrize(
        "raw",
        [r"\\server\share\file.wav", r"\\?\UNC\server\share\file.wav", r"\\.\PhysicalDrive0", r"\\?\C:\asset.wav"],
    )
    def test_unc_forms_never_call_resolve(self, raw, monkeypatch):
        module = importlib.import_module("cli_anything.gpt_sovits.core.paths")
        resolve_calls = []

        def forbidden_resolve(candidate, *args, **kwargs):
            resolve_calls.append(str(candidate))
            raise NetworkTouched(str(candidate))

        monkeypatch.setattr(Path, "resolve", forbidden_resolve)
        with pytest.raises(CLIError) as raised:
            module.require_local_path(raw, purpose="round3_unc")
        assert raised.value.code == "nonlocal_path"
        assert resolve_calls == []

    @pytest.mark.parametrize("profile", [r"\\server\share", r"\\?\UNC\server\share", r"\\.\device"])
    def test_expanduser_nonlocal_forms_never_call_resolve(self, profile, monkeypatch):
        module = importlib.import_module("cli_anything.gpt_sovits.core.paths")
        resolve_calls = []

        def forbidden_resolve(candidate, *args, **kwargs):
            resolve_calls.append(str(candidate))
            raise NetworkTouched(str(candidate))

        monkeypatch.setenv("USERPROFILE", profile)
        monkeypatch.setattr(Path, "resolve", forbidden_resolve)
        with pytest.raises(CLIError) as raised:
            module.require_local_path(r"~\asset.wav", purpose="round4_expanded_nonlocal")
        assert raised.value.code == "nonlocal_path"
        assert resolve_calls == []

    def test_expanduser_local_profile_still_resolves_normally(self, tmp_path, monkeypatch):
        module = importlib.import_module("cli_anything.gpt_sovits.core.paths")
        local_profile = tmp_path / "local-profile"
        local_profile.mkdir()
        expected = (local_profile / "asset.wav").resolve()
        resolve_calls = []
        original_resolve = Path.resolve

        def record_resolve(candidate, *args, **kwargs):
            resolve_calls.append(str(candidate))
            return original_resolve(candidate, *args, **kwargs)

        monkeypatch.setenv("USERPROFILE", str(local_profile))
        monkeypatch.setattr(Path, "resolve", record_resolve)
        assert module.require_local_path(r"~\asset.wav", purpose="round4_local_home") == expected
        assert resolve_calls == [str(local_profile / "asset.wav")]

    @pytest.mark.parametrize("raw", [r"C:\local\asset.wav", r"D:\本地 路径\asset.wav"])
    def test_local_absolute_drive_is_checked_before_and_after_resolve(self, raw, monkeypatch):
        module = importlib.import_module("cli_anything.gpt_sovits.core.paths")
        drive_checks = []
        resolve_calls = []

        def local_drive(root):
            drive_checks.append(str(root).upper())
            return 3

        def lexical_resolve(candidate, *args, **kwargs):
            resolve_calls.append(str(candidate))
            return Path(raw)

        monkeypatch.setattr(module, "_get_drive_type", local_drive)
        monkeypatch.setattr(Path, "resolve", lexical_resolve)
        assert module.require_local_path(raw, purpose="round3_local") == Path(raw)
        assert resolve_calls == [raw]
        expected_root = raw[:3].upper()
        assert drive_checks == [expected_root, expected_root]

    def test_relative_local_path_is_checked_after_resolve(self, monkeypatch):
        module = importlib.import_module("cli_anything.gpt_sovits.core.paths")
        drive_checks = []
        resolve_calls = []

        monkeypatch.setattr(module, "_get_drive_type", lambda root: drive_checks.append(str(root).upper()) or 3)

        def local_resolve(candidate, *args, **kwargs):
            resolve_calls.append(str(candidate))
            return Path(r"D:\local\relative.wav")

        monkeypatch.setattr(Path, "resolve", local_resolve)
        assert module.require_local_path("relative.wav", purpose="round3_relative") == Path(r"D:\local\relative.wav")
        assert resolve_calls == ["relative.wav"]
        assert drive_checks == ["D:\\"]

    def test_resolved_remote_target_is_rejected_by_second_check(self, monkeypatch):
        module = importlib.import_module("cli_anything.gpt_sovits.core.paths")
        resolve_calls = []

        def redirected_resolve(candidate, *args, **kwargs):
            resolve_calls.append(str(candidate))
            return Path(r"Z:\remote-reparse\asset.wav")

        monkeypatch.setattr(Path, "resolve", redirected_resolve)
        monkeypatch.setattr(
            module,
            "_get_drive_type",
            lambda root: module.DRIVE_REMOTE if str(root).upper() == "Z:\\" else 3,
        )
        with pytest.raises(CLIError) as raised:
            module.require_local_path("relative.wav", purpose="round3_reparse")
        assert raised.value.code == "nonlocal_path"
        assert resolve_calls == ["relative.wav"]


class _FakeDownloadResponse(io.BytesIO):
    def __init__(self, payload: bytes, final_url: str):
        super().__init__(payload)
        self._final_url = final_url
        self.headers = {"Content-Length": str(len(payload))}

    def geturl(self):
        return self._final_url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class TestUVRDownload:
    def test_redirect_handler_rejects_unapproved_host_before_following(self):
        module = importlib.import_module("cli_anything.gpt_sovits.core.uvr")
        handler = module._ApprovedRedirectHandler()
        with pytest.raises(CLIError) as raised:
            handler.redirect_request(
                None,
                None,
                302,
                "Found",
                {},
                "https://evil.example/object?token=secret",
            )
        assert raised.value.code == "uvr_redirect_forbidden"
        assert "secret" not in str(raised.value.details)

    def test_stream_download_records_redacted_final_url_size_and_hash(self, tmp_path):
        payload = b"approved archive bytes"
        digest = hashlib.sha256(payload).hexdigest()
        response = _FakeDownloadResponse(payload, "https://us.aws.cdn.hf.co/object/content?X-Amz-Signature=secret")
        module = importlib.import_module("cli_anything.gpt_sovits.core.uvr")
        result = module.download_uvr_archive(
            APPROVED_UVR_URL,
            tmp_path / "uvr.zip",
            expected_size=len(payload),
            expected_sha256=digest,
            transport=lambda _url: response,
        )
        assert result["final_url"] == "https://us.aws.cdn.hf.co/object/content"
        assert result["bytes"] == len(payload)
        assert result["sha256"] == digest
        assert (tmp_path / "uvr.zip").read_bytes() == payload

    @pytest.mark.parametrize(
        "final_url,expected_hash",
        [("https://evil.example/object", None), ("https://huggingface.co/object", "0" * 64)],
    )
    def test_download_rejects_bad_final_host_or_hash_and_cleans_temp(self, tmp_path, final_url, expected_hash):
        payload = b"payload"
        module = importlib.import_module("cli_anything.gpt_sovits.core.uvr")
        with pytest.raises(CLIError):
            module.download_uvr_archive(
                APPROVED_UVR_URL,
                tmp_path / "uvr.zip",
                expected_size=len(payload),
                expected_sha256=expected_hash,
                transport=lambda _url: _FakeDownloadResponse(payload, final_url),
            )
        assert not (tmp_path / "uvr.zip").exists()
        assert not list(tmp_path.glob(".uvr.zip.*.tmp"))

    def test_download_dry_run_and_default_no_overwrite(self, tmp_path):
        module = importlib.import_module("cli_anything.gpt_sovits.core.uvr")
        output = tmp_path / "uvr.zip"
        result = module.download_uvr_archive(
            APPROVED_UVR_URL,
            output,
            dry_run=True,
            transport=lambda _url: pytest.fail("dry-run opened transport"),
        )
        assert result["dry_run"] is True and not output.exists()
        output.write_bytes(b"sentinel")
        with pytest.raises(CLIError) as raised:
            module.download_uvr_archive(APPROVED_UVR_URL, output, transport=lambda _url: pytest.fail("overwrote"))
        assert raised.value.code == "output_exists"


class TestVersionedWorkflow:
    def test_asr_segments_are_clamped_and_corrections_recorded(self):
        module = importlib.import_module("cli_anything.gpt_sovits.core.workflow")
        result = module.clamp_asr_segments(
            [{"start": -0.2, "end": 1.0, "text": "a"}, {"start": 1.0, "end": 4.94, "text": "b"}],
            duration=3.0,
        )
        assert result["clamp_count"] == 2
        assert result["segments"][0]["start"] == 0
        assert result["segments"][1]["end"] == 3.0
        assert all(item["start"] <= item["end"] <= 3.0 for item in result["segments"])
        assert all("original_start" in item or "original_end" in item for item in result["segments"])

    @pytest.mark.parametrize("duration", [Fraction(4_000_001, 4_000_000), Fraction(2_000_003, 2_000_000)])
    def test_asr_serialization_never_rounds_above_sub_microsecond_boundary(self, duration):
        module = importlib.import_module("cli_anything.gpt_sovits.core.workflow")
        result = module.clamp_asr_segments([{"start": 0, "end": 2.0, "text": "boundary"}], duration=duration)
        assert result["segments"][0]["end"] <= duration

    def test_wav_frame_duration_is_exact_rational(self, tmp_path):
        module = importlib.import_module("cli_anything.gpt_sovits.core.workflow")
        audio = make_wav(tmp_path / "quarter-microsecond.wav", seconds=0.00000025, rate=4_000_000)
        boundary = module.wav_frame_duration(audio)
        assert boundary == Fraction(1, 4_000_000)

    @pytest.mark.parametrize("operation", ["transcribe", "prepare", "index", "uvr_compare"])
    def test_versioned_workflow_writes_are_dry_run_and_no_overwrite(self, tmp_path, operation):
        module = importlib.import_module("cli_anything.gpt_sovits.core.workflow")
        checker = getattr(module, f"{operation}_plan")
        output = tmp_path / f"{operation}.json"
        plan = checker(output=output, dry_run=True)
        assert plan["dry_run"] is True
        assert not output.exists()
        output.write_bytes(b"sentinel")
        with pytest.raises(CLIError) as raised:
            checker(output=output)
        assert raised.value.code == "output_exists"


class TestProofreadingIndex:
    @staticmethod
    def _fixture(tmp_path, *, status="pending", proposals=None):
        audio = make_wav(tmp_path / "speaker-001.wav", seconds=2, rate=32000)
        manifest = tmp_path / "manifest.jsonl"
        manifest.write_text(json.dumps(_record(audio, review_status=status), ensure_ascii=False) + "\n", encoding="utf-8")
        proposal_path = tmp_path / "proofreading.json"
        payload = proposals or {
            "version": 1,
            "records": [
                {
                    "id": "speaker-001",
                    "text_zh": "<script>中文字幕</script>",
                    "text_ja_proposed": "ワトソン博士、大丈夫ですか。",
                    "confidence": "high",
                    "notes": "博士名を修正 & 要确认",
                }
            ],
        }
        proposal_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return manifest, proposal_path

    def test_renders_audio_draft_proposal_and_escaped_context(self, tmp_path):
        manifest, proposals = self._fixture(tmp_path)
        output = tmp_path / "日语校对索引.html"
        result = build_proofreading_index(manifest, proposals, output)
        html = output.read_text(encoding="utf-8")
        assert result["count"] == 1
        assert result["confidence_counts"] == {"high": 1, "medium": 0, "listen": 0}
        assert result["total_duration_seconds"] == 2.0
        assert "テストです" in html and "ワトソン博士、大丈夫ですか。" in html
        assert "&lt;script&gt;中文字幕&lt;/script&gt;" in html and "<script>中文字幕</script>" not in html
        assert "博士名を修正 &amp; 要确认" in html
        assert "speaker-001.wav" in html

    def test_dry_run_has_no_side_effect(self, tmp_path):
        manifest, proposals = self._fixture(tmp_path)
        output = tmp_path / "new" / "日语校对索引.html"
        result = build_proofreading_index(manifest, proposals, output, dry_run=True)
        assert result["dry_run"] is True and result["count"] == 1
        assert not output.parent.exists()

    def test_existing_output_requires_overwrite(self, tmp_path):
        manifest, proposals = self._fixture(tmp_path)
        output = tmp_path / "日语校对索引.html"
        output.write_text("sentinel", encoding="utf-8")
        with pytest.raises(CLIError) as raised:
            build_proofreading_index(manifest, proposals, output)
        assert raised.value.code == "output_exists"
        assert output.read_text(encoding="utf-8") == "sentinel"

    @pytest.mark.parametrize(
        "payload,code",
        [
            ({"version": 1, "records": []}, "invalid_proofreading_input"),
            ({"version": 1, "records": [{"id": "unknown", "text_zh": "中", "text_ja_proposed": "日", "confidence": "high"}]}, "unknown_proofreading_id"),
            ({"version": 1, "records": [{"id": "speaker-001", "text_zh": "", "text_ja_proposed": "日", "confidence": "high"}]}, "invalid_proofreading_record"),
            ({"version": 1, "records": [{"id": "speaker-001", "text_zh": "中", "text_ja_proposed": "日", "confidence": "certain"}]}, "invalid_proofreading_confidence"),
            ({"version": 1, "records": [{"id": "speaker-001", "text_zh": "中", "text_ja_proposed": "日", "confidence": "high"}, {"id": "speaker-001", "text_zh": "中", "text_ja_proposed": "日", "confidence": "high"}]}, "duplicate_proofreading_id"),
        ],
    )
    def test_rejects_invalid_proposals(self, tmp_path, payload, code):
        manifest, proposals = self._fixture(tmp_path, proposals=payload)
        with pytest.raises(CLIError) as raised:
            build_proofreading_index(manifest, proposals, tmp_path / "out.html")
        assert raised.value.code == code

    def test_rejects_non_pending_manifest_record(self, tmp_path):
        manifest, proposals = self._fixture(tmp_path, status="rejected")
        with pytest.raises(CLIError) as raised:
            build_proofreading_index(manifest, proposals, tmp_path / "out.html")
        assert raised.value.code == "proofreading_status_mismatch"

    def test_cli_dry_run_and_real_json_output(self, tmp_path):
        manifest, proposals = self._fixture(tmp_path)
        output = tmp_path / "日语校对索引.html"
        args = ["dataset", "proofread-index", "--manifest", str(manifest), "--proposals", str(proposals), "--output", str(output), "--json"]
        dry = CliRunner().invoke(cli, args + ["--dry-run"])
        assert dry.exit_code == 0, dry.output
        assert json.loads(dry.output)["data"]["dry_run"] is True
        assert not output.exists()
        real = CliRunner().invoke(cli, args)
        assert real.exit_code == 0, real.output
        assert json.loads(real.output)["data"]["count"] == 1
        assert output.is_file()


class TestListeningIndexReviewLabels:
    def test_approved_text_and_status_summary_are_truthful(self, tmp_path):
        approved_audio = make_wav(tmp_path / "approved.wav", seconds=2, rate=32000)
        rejected_audio = make_wav(tmp_path / "rejected.wav", seconds=2, rate=32000, amplitude=800)
        rows = [
            _record(approved_audio, text_ja="確認済みです", review_status="approved"),
            _record(rejected_audio, text_ja="却下された草稿", review_status="rejected"),
        ]
        manifest = tmp_path / "manifest.jsonl"
        manifest.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
        output = tmp_path / "试听索引.html"
        result = build_listening_index(manifest, output)
        html = output.read_text(encoding="utf-8")
        assert result["status_counts"] == {"approved": 1, "pending": 0, "rejected": 1}
        assert "已确认日语" in html and "历史日语草稿" in html
        assert "approved 1" in html and "pending 0" in html and "rejected 1" in html
        assert "当前 2 条仍需逐条核对" not in html


class TestTestDocumentationMirror:
    def test_canonical_and_packaged_test_docs_match_byte_for_byte(self):
        canonical = Path(__file__).parents[4] / "tests" / "TEST.md"
        packaged = Path(__file__).with_name("TEST.md")
        assert hashlib.sha256(canonical.read_bytes()).hexdigest() == hashlib.sha256(packaged.read_bytes()).hexdigest()


class TestPhase2ACLI:
    def test_help_lists_training_and_dataset_groups(self):
        result = CliRunner().invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "training" in result.output and "dataset" in result.output

    @pytest.mark.parametrize(
        "args,command",
        [
            (["training", "--help"], "download-uvr"),
            (["dataset", "--help"], "transcribe"),
            (["dataset", "--help"], "prepare"),
            (["dataset", "--help"], "index"),
            (["dataset", "--help"], "uvr-compare"),
            (["dataset", "--help"], "proofread-index"),
        ],
    )
    def test_help_lists_versioned_phase2a_workflows(self, args, command):
        result = CliRunner().invoke(cli, args)
        assert result.exit_code == 0
        assert command in result.output

    def test_dataset_extract_dry_run_json(self, tmp_path):
        source = make_wav(tmp_path / "来源.wav", seconds=3)
        output = tmp_path / "new" / "候选.wav"
        result = CliRunner().invoke(
            cli,
            ["dataset", "extract", "--source", str(source), "--start", "0.5", "--end", "2.5", "--output", str(output), "--dry-run", "--json"],
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["dry_run"] is True
        assert not output.parent.exists()

    def test_dataset_inspect_json(self, tmp_path):
        audio = make_wav(tmp_path / "候选.wav", seconds=2, rate=32000)
        result = CliRunner().invoke(cli, ["dataset", "inspect", str(audio), "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["reports"][0]["compliant"] is True

    def test_dataset_inspect_reports_empty_annotation(self, tmp_path):
        audio = make_wav(tmp_path / "候选.wav", seconds=2, rate=32000)
        result = CliRunner().invoke(
            cli,
            ["dataset", "inspect", str(audio), "--text-ja", "", "--json"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["annotations"] == {"checked": True, "empty_indices": [0], "compliant": False}
        assert data["compliant"] is False

    def test_dataset_inspect_annotation_count_must_match_audio(self, tmp_path):
        first = make_wav(tmp_path / "one.wav", seconds=2, rate=32000)
        second = make_wav(tmp_path / "two.wav", seconds=2, rate=32000)
        result = CliRunner().invoke(
            cli,
            ["dataset", "inspect", str(first), str(second), "--text-ja", "テスト", "--json"],
        )
        assert result.exit_code != 0
        assert json.loads(result.output)["error"]["code"] == "annotation_count_mismatch"

    def test_dataset_manifest_reads_utf8_records(self, tmp_path):
        audio = make_wav(tmp_path / "候选.wav", seconds=2, rate=32000)
        records = tmp_path / "records.json"
        records.write_text(json.dumps([_record(audio)], ensure_ascii=False), encoding="utf-8")
        output = tmp_path / "manifest.jsonl"
        result = CliRunner().invoke(cli, ["dataset", "manifest", "--records", str(records), "--output", str(output), "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(output.read_text(encoding="utf-8"))["text_ja"] == "テストです"

    def test_zip_rejects_symbolic_links(self, tmp_path):
        archive = tmp_path / "symlink.zip"
        link = zipfile.ZipInfo("link.pth")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(archive, "w") as stream:
            stream.writestr(link, "target.pth")
        with pytest.raises(CLIError) as raised:
            safe_extract_uvr_zip(archive, tmp_path / "weights")
        assert raised.value.code == "unsafe_archive"
