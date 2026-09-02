from __future__ import annotations

import json
import math
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from cli_anything.gpt_sovits.core.audio import inspect_wav
from cli_anything.gpt_sovits.core.config import Settings, _default_runtime, ensure_local_api_url
from cli_anything.gpt_sovits.core.errors import CLIError
from cli_anything.gpt_sovits.core.models import list_models, validate_weight
from cli_anything.gpt_sovits.core.output import envelope
from cli_anything.gpt_sovits.core.state import load_json, locked_append_json_line, locked_save_json
from cli_anything.gpt_sovits.core import service
from cli_anything.gpt_sovits.core.doctor import run_doctor
from cli_anything.gpt_sovits.gpt_sovits_cli import _load_text, _split_repl_line, cli


def make_wav(path: Path, seconds: float = 0.1, rate: int = 16000, amplitude: int = 4000) -> Path:
    samples = [int(amplitude * math.sin(2 * math.pi * 440 * i / rate)) for i in range(int(seconds * rate))]
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))
    return path


class TestConfig:
    def test_explicit_paths_are_resolved(self, tmp_path):
        settings = Settings.discover(str(tmp_path), str(tmp_path / "python.exe"), state_dir=str(tmp_path / "state"))
        assert settings.checkout == tmp_path.resolve()

    def test_environment_overrides(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GPT_SOVITS_CHECKOUT", str(tmp_path))
        settings = Settings.discover(state_dir=str(tmp_path / "state"))
        assert settings.checkout == tmp_path.resolve()

    def test_explicit_checkout_overrides_environment(self, tmp_path, monkeypatch):
        environment_checkout = tmp_path / "environment"
        explicit_checkout = tmp_path / "explicit"
        monkeypatch.setenv("GPT_SOVITS_CHECKOUT", str(environment_checkout))
        settings = Settings.discover(checkout=str(explicit_checkout), state_dir=str(tmp_path / "state"))
        assert settings.checkout == explicit_checkout.resolve()

    def test_default_checkout_uses_user_home(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GPT_SOVITS_CHECKOUT", raising=False)
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        settings = Settings.discover(state_dir=str(tmp_path / "state"))
        assert settings.checkout == (tmp_path / "GPT-SoVITS").resolve()

    def test_explicit_runtime_overrides_environment(self, tmp_path, monkeypatch):
        environment_runtime = tmp_path / "environment-python"
        explicit_runtime = tmp_path / "explicit-python"
        monkeypatch.setenv("GPT_SOVITS_RUNTIME", str(environment_runtime))
        settings = Settings.discover(
            checkout=str(tmp_path / "checkout"),
            runtime=str(explicit_runtime),
            state_dir=str(tmp_path / "state"),
        )
        assert settings.runtime == explicit_runtime.resolve()

    def test_default_runtime_is_platform_specific(self, tmp_path):
        checkout = tmp_path / "checkout"
        assert _default_runtime(checkout, "nt") == checkout / ".conda" / "python.exe"
        assert _default_runtime(checkout, "posix") == checkout / ".conda" / "bin" / "python"

    @pytest.mark.parametrize("url", ["http://127.0.0.1:9880", "http://localhost:9880", "http://[::1]:9880"])
    def test_loopback_urls_allowed(self, url):
        assert ensure_local_api_url(url) == url

    @pytest.mark.parametrize("url", ["http://192.168.1.2:9880", "https://example.com:443", "127.0.0.1:9880", "http://127.0.0.1"])
    def test_nonlocal_or_incomplete_urls_rejected(self, url):
        with pytest.raises(CLIError):
            ensure_local_api_url(url)

    def test_backend_validation_lists_missing(self, tmp_path):
        settings = Settings.discover(str(tmp_path / "missing"), str(tmp_path / "python.exe"), state_dir=str(tmp_path))
        with pytest.raises(CLIError) as raised:
            settings.validate_backend()
        assert raised.value.code == "backend_missing"
        assert raised.value.details["missing"]


class TestPublicationIgnoreBoundary:
    def test_private_assets_are_ignored_across_the_cli_subtree(self):
        repository = Path(__file__).resolve().parents[6]
        protected = [
            "CLI/gpt-sovits/models/example.ckpt",
            "CLI/gpt-sovits/agent-harness/tmp/example.safetensors",
            "CLI/gpt-sovits/docs/private/example.wav",
            "CLI/gpt-sovits/runtime/state/session.json",
            "CLI/gpt-sovits/data/reference-prompt.json",
        ]
        for candidate in protected:
            result = subprocess.run(
                ["git", "check-ignore", "--no-index", "--quiet", candidate],
                cwd=repository,
                check=False,
            )
            assert result.returncode == 0, candidate

        source = "CLI/gpt-sovits/agent-harness/cli_anything/gpt_sovits/core/config.py"
        ignored = subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", source],
            cwd=repository,
            check=False,
        )
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", source],
            cwd=repository,
            capture_output=True,
            text=True,
            check=False,
        )
        assert ignored.returncode == 1
        assert tracked.returncode == 0


class TestTextAndOutput:
    def test_direct_text(self):
        assert _load_text(" hello ", None) == "hello"

    def test_utf8_text_file(self, tmp_path):
        path = tmp_path / "中文 文本.txt"
        path.write_text("你好", encoding="utf-8")
        assert _load_text(None, str(path)) == "你好"

    @pytest.mark.parametrize("text,text_file", [(None, None), ("a", "b")])
    def test_exactly_one_text_source(self, text, text_file):
        with pytest.raises(CLIError):
            _load_text(text, text_file)

    def test_empty_text_file_rejected(self, tmp_path):
        path = tmp_path / "empty.txt"
        path.write_text("  ", encoding="utf-8")
        with pytest.raises(CLIError) as raised:
            _load_text(None, str(path))
        assert raised.value.code == "empty_text"

    def test_invalid_utf8_rejected(self, tmp_path):
        path = tmp_path / "bad.txt"
        path.write_bytes(b"\xff\xfe")
        with pytest.raises(CLIError) as raised:
            _load_text(None, str(path))
        assert raised.value.code == "invalid_text_encoding"

    def test_success_envelope_shape(self):
        result = envelope("doctor", data={"ready": True})
        assert set(result) == {"ok", "command", "data", "warnings", "error"}
        assert result["ok"] is True and result["error"] is None

    def test_error_envelope_shape(self):
        result = envelope("x", error={"code": "bad"})
        assert result["ok"] is False and result["data"] is None

class TestAudio:
    def test_valid_wav_metadata(self, tmp_path):
        report = inspect_wav(make_wav(tmp_path / "tone.wav"))
        assert report["format"] == "wav"
        assert report["sample_rate"] == 16000
        assert report["channels"] == 1
        assert report["rms"] > 10
        assert len(report["sha256"]) == 64

    def test_non_wav_rejected(self, tmp_path):
        path = tmp_path / "fake.wav"
        path.write_text("not audio")
        with pytest.raises(CLIError) as raised:
            inspect_wav(path)
        assert raised.value.code == "invalid_wav"

    def test_missing_audio_rejected(self, tmp_path):
        with pytest.raises(CLIError) as raised:
            inspect_wav(tmp_path / "none.wav")
        assert raised.value.code == "audio_not_found"

    def test_silence_rejected_when_required(self, tmp_path):
        with pytest.raises(CLIError) as raised:
            inspect_wav(make_wav(tmp_path / "silent.wav", amplitude=0), require_non_silent=True)
        assert raised.value.code == "silent_audio"


class TestModelsAndState:
    def test_model_listing(self, tmp_path):
        (tmp_path / "GPT_weights_v2").mkdir()
        (tmp_path / "SoVITS_weights_v2").mkdir()
        (tmp_path / "GPT_weights_v2" / "a.ckpt").write_bytes(b"x")
        (tmp_path / "SoVITS_weights_v2" / "b.pth").write_bytes(b"x")
        settings = Settings.discover(str(tmp_path), str(tmp_path / "python.exe"), state_dir=str(tmp_path / "state"))
        result = list_models(settings)
        assert result["gpt"][0].endswith("a.ckpt")
        assert result["sovits"][0].endswith("b.pth")

    @pytest.mark.parametrize("kind,suffix", [("gpt", ".ckpt"), ("sovits", ".pth")])
    def test_validate_weight(self, tmp_path, kind, suffix):
        path = tmp_path / f"model{suffix}"
        path.write_bytes(b"x")
        assert validate_weight(str(path), kind) == path.resolve()

    def test_wrong_weight_type(self, tmp_path):
        path = tmp_path / "model.pth"
        path.write_bytes(b"x")
        with pytest.raises(CLIError) as raised:
            validate_weight(str(path), "gpt")
        assert raised.value.code == "wrong_weight_type"

    def test_locked_json_roundtrip(self, tmp_path):
        path = tmp_path / "state" / "service.json"
        locked_save_json(path, {"pid": 123, "中文": True})
        assert load_json(path) == {"pid": 123, "中文": True}

    def test_locked_utf8_jsonl_append(self, tmp_path):
        path = tmp_path / "state" / "api.log"
        locked_append_json_line(path, {"event": "服务就绪", "pid": 123})
        assert json.loads(path.read_text(encoding="utf-8", errors="strict")) == {"event": "服务就绪", "pid": 123}


class TestCLIUnit:
    def test_doctor_json_contract_with_missing_backend(self, tmp_path):
        result = CliRunner().invoke(cli, ["--checkout", str(tmp_path), "--runtime", str(tmp_path / "python.exe"), "--state-dir", str(tmp_path / "state"), "doctor", "--json"])
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["ok"] is True and body["command"] == "doctor"
        assert body["data"]["ready"] is False

    def test_synthesize_dry_run_has_no_output(self, tmp_path):
        ref = make_wav(tmp_path / "参考.wav")
        output = tmp_path / "输出.wav"
        result = CliRunner().invoke(cli, ["synthesize", "--text", "测试", "--text-lang", "zh", "--ref-audio", str(ref), "--prompt-lang", "zh", "--output", str(output), "--dry-run", "--json"])
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["data"]["dry_run"] is True
        assert not output.exists()

    def test_bad_parameter_boundary(self, tmp_path):
        ref = make_wav(tmp_path / "ref.wav")
        result = CliRunner().invoke(cli, ["synthesize", "--text", "x", "--text-lang", "en", "--ref-audio", str(ref), "--prompt-lang", "en", "--output", str(tmp_path / "o.wav"), "--top-p", "0", "--dry-run"])
        assert result.exit_code != 0

    def test_reference_inspect_json(self, tmp_path):
        ref = make_wav(tmp_path / "ref.wav")
        result = CliRunner().invoke(cli, ["reference", "inspect", str(ref), "--json"])
        assert result.exit_code == 0
        assert json.loads(result.output)["data"]["sha256"]

    def test_repl_split_preserves_windows_spaces_and_chinese(self):
        path = r"D:\语音 文件\参考 音频.wav"
        tokens = _split_repl_line(f'reference inspect "{path}" --json')
        assert tokens == ["reference", "inspect", path, "--json"]


class TestServiceSafety:
    def _settings(self, tmp_path):
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / "api_v2.py").write_text("# test", encoding="utf-8")
        runtime = tmp_path / "python.exe"
        runtime.write_bytes(b"x")
        config = checkout / "tts.yaml"
        config.write_text("custom: {}", encoding="utf-8")
        return Settings.discover(str(checkout), str(runtime), tts_config=str(config), state_dir=str(tmp_path / "state"))

    def _real_settings(self, tmp_path, script_body="import time; time.sleep(60)"):
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / "api_v2.py").write_text(script_body, encoding="utf-8")
        config = checkout / "tts.yaml"
        config.write_text("custom: {}", encoding="utf-8")
        return Settings.discover(str(checkout), sys.executable, api_url="http://127.0.0.1:65431", tts_config=str(config), state_dir=str(tmp_path / "state"))

    def _spawn_recorded_process(self, settings):
        config_copy = settings.state_dir / "runtime" / "tts_infer.yaml"
        config_copy.parent.mkdir(parents=True)
        config_copy.write_text("custom: {}", encoding="utf-8")
        command = service._expected_command(settings, config_copy)
        process = subprocess.Popen(command, cwd=settings.checkout)
        record = {
            "pid": process.pid,
            "runtime": str(settings.runtime),
            "api_script": str(settings.api_script),
            "tts_config": str(config_copy),
            "bind_addr": "127.0.0.1",
            "port": 65431,
            "command": command,
            "process_create_time": service.psutil.Process(process.pid).create_time(),
        }
        return process, record

    def test_stop_refuses_without_record(self, tmp_path):
        with pytest.raises(CLIError) as raised:
            service.stop(self._settings(tmp_path))
        assert raised.value.code == "not_managed"

    def test_stop_refuses_identity_mismatch(self, tmp_path, monkeypatch):
        settings = self._settings(tmp_path)
        locked_save_json(settings.state_dir / "service.json", {"pid": 123})
        monkeypatch.setattr(service, "verify_process", lambda record: (False, "mismatch"))
        with pytest.raises(CLIError) as raised:
            service.stop(settings)
        assert raised.value.code == "identity_mismatch"

    def test_stop_dry_run_does_not_terminate(self, tmp_path, monkeypatch):
        settings = self._settings(tmp_path)
        locked_save_json(settings.state_dir / "service.json", {"pid": 123})
        monkeypatch.setattr(service, "verify_process", lambda record: (True, "ok"))
        result = service.stop(settings, dry_run=True)
        assert result["dry_run"] is True
        assert (settings.state_dir / "service.json").exists()

    def test_start_is_idempotent_for_owned_process(self, tmp_path, monkeypatch):
        settings = self._settings(tmp_path)
        monkeypatch.setattr(service, "status", lambda value: {"managed": True, "running": True, "pid": 7, "api": {"reachable": True}})
        result = service.start(settings, dry_run=True)
        assert result["action"] == "already_running"

    def test_start_dry_run_declares_discard_policy_without_creating_log(self, tmp_path, monkeypatch):
        settings = self._settings(tmp_path)
        monkeypatch.setattr(service, "status", lambda value: {"managed": False, "running": False, "api": {"reachable": False}})
        result = service.start(settings, dry_run=True)
        assert result["output_policy"] == "discard"
        assert not (settings.state_dir / "api.log").exists()

    def test_legacy_content_filter_is_absent(self):
        package_root = Path(service.__file__).resolve().parents[1]
        assert not (package_root / "utils" / "log_filter.py").exists()

    def test_failed_start_removes_stale_record(self, tmp_path, monkeypatch):
        settings = self._real_settings(tmp_path, "raise SystemExit(2)")
        monkeypatch.setattr(service, "status", lambda value: {"managed": False, "running": False, "api": {"reachable": False}})
        monkeypatch.setattr(service, "api_probe", lambda *args, **kwargs: {"reachable": False})
        with pytest.raises(CLIError) as raised:
            service.start(settings, timeout=5)
        assert raised.value.code == "service_start_failed"
        assert not (settings.state_dir / "service.json").exists()

    def test_real_temporary_process_identity_matches(self, tmp_path):
        settings = self._real_settings(tmp_path)
        process, record = self._spawn_recorded_process(settings)
        try:
            matched, reason = service.verify_process(record)
            assert matched is True, reason
        finally:
            process.terminate()
            process.wait(timeout=10)

    @pytest.mark.parametrize("mutation", ["create_time", "port", "config", "order"])
    def test_real_process_identity_rejects_reuse_or_argument_mismatch(self, tmp_path, mutation):
        settings = self._real_settings(tmp_path)
        process, record = self._spawn_recorded_process(settings)
        try:
            if mutation == "create_time":
                record["process_create_time"] -= 100
            elif mutation == "port":
                record["command"][5] = "65432"
            elif mutation == "config":
                record["command"][7] = str(tmp_path / "other.yaml")
            else:
                record["command"][2:6] = ["-p", "65431", "-a", "127.0.0.1"]
            matched, _ = service.verify_process(record)
            assert matched is False
        finally:
            process.terminate()
            process.wait(timeout=10)

    def test_state_save_failure_terminates_real_spawned_process(self, tmp_path, monkeypatch):
        settings = self._real_settings(tmp_path)
        spawned = []
        spawned_kwargs = []
        real_popen = subprocess.Popen

        def capture_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            spawned.append(process)
            spawned_kwargs.append(kwargs)
            return process

        monkeypatch.setattr(service, "status", lambda value: {"managed": False, "running": False, "api": {"reachable": False}})
        monkeypatch.setattr(service.subprocess, "Popen", capture_popen)
        monkeypatch.setattr(service, "locked_save_json", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(CLIError) as raised:
            service.start(settings, timeout=5)
        assert raised.value.code == "state_save_failed"
        assert len(spawned) == 1
        for process in spawned:
            process.wait(timeout=10)
            assert process.poll() is not None
        assert spawned_kwargs[0]["stdout"] == subprocess.DEVNULL
        assert spawned_kwargs[0]["stderr"] == subprocess.DEVNULL
        assert spawned_kwargs[0]["env"]["PYTHONUTF8"] == "1"
        assert spawned_kwargs[0]["env"]["PYTHONIOENCODING"] == "utf-8"
        assert spawned_kwargs[0]["env"]["PYTHONUNBUFFERED"] == "1"

    def test_service_discards_all_backend_output_and_logs_only_lifecycle(self, tmp_path, monkeypatch):
        markers = [f"后端秘密-{index}-甲乙" for index in range(8)]
        script_body = (
            "import time\n"
            f"print('实际输入的参考文本: {markers[0]}', flush=True)\n"
            "print('', flush=True)\n"
            "print('############ user supplied ############', flush=True)\n"
            f"print('{markers[1]}', flush=True)\n"
            "print('######## 任意伪造诊断标题 ########', flush=True)\n"
            f"print('{markers[2]}', flush=True)\n"
            f"print('{{\"event\":\"service_ready\",\"secret\":\"{markers[3]}\"}}', flush=True)\n"
            f"print('INFO injected {markers[4]}', flush=True)\n"
            f"print('WARNING injected {markers[5]}', flush=True)\n"
            "print('实际输入的目标文本:', flush=True)\n"
            f"print('{markers[6]}\\n{markers[7]}', flush=True)\n"
            "time.sleep(60)\n"
        )
        settings = self._real_settings(tmp_path, script_body)
        monkeypatch.setattr(service, "status", lambda value: {"managed": False, "running": False, "api": {"reachable": False}})
        monkeypatch.setattr(service, "api_probe", lambda *args, **kwargs: {"reachable": False})
        with pytest.raises(CLIError) as raised:
            service.start(settings, timeout=0.1)
        assert raised.value.code == "service_start_timeout"
        artifact_bytes = b"".join(path.read_bytes() for path in settings.state_dir.rglob("*") if path.is_file())
        assert all(marker.encode("utf-8") not in artifact_bytes for marker in markers)
        persisted = (settings.state_dir / "api.log").read_text(encoding="utf-8", errors="strict")
        events = [json.loads(line) for line in persisted.splitlines()]
        assert {event["event"] for event in events} >= {"start_requested", "spawned", "start_timeout"}
        assert next(event for event in events if event["event"] == "start_requested")["output_policy"] == "discard"


class TestDoctorReadiness:
    def _ready_settings(self, tmp_path, device="cuda", configured_missing=False):
        checkout = tmp_path / "checkout"
        (checkout / "GPT_SoVITS" / "configs").mkdir(parents=True)
        (checkout / "GPT_weights_v2").mkdir()
        (checkout / "SoVITS_weights_v2").mkdir()
        (checkout / "api_v2.py").write_text("# test", encoding="utf-8")
        runtime = checkout / "python.exe"
        runtime.write_bytes(b"x")
        gpt = checkout / "GPT_weights_v2" / "gpt.ckpt"
        sovits = checkout / "SoVITS_weights_v2" / "sovits.pth"
        bert = checkout / "bert"
        hubert = checkout / "hubert"
        gpt.write_bytes(b"x")
        sovits.write_bytes(b"x")
        bert.mkdir()
        hubert.mkdir()
        configured_gpt = checkout / "missing.ckpt" if configured_missing else gpt
        config = checkout / "GPT_SoVITS" / "configs" / "tts_infer.yaml"
        config.write_text(
            "custom:\n"
            f"  device: {device}\n"
            f"  bert_base_path: {bert}\n"
            f"  cnhuhbert_base_path: {hubert}\n"
            f"  t2s_weights_path: {configured_gpt}\n"
            f"  vits_weights_path: {sovits}\n",
            encoding="utf-8",
        )
        return Settings.discover(str(checkout), str(runtime), tts_config=str(config), state_dir=str(tmp_path / "state"))

    @pytest.mark.parametrize(
        "device,cuda,expected",
        [("cuda", False, False), ("cuda:0", False, False), ("cuda", True, True), ("cpu", False, True)],
    )
    def test_ready_respects_configured_device(self, tmp_path, monkeypatch, device, cuda, expected):
        settings = self._ready_settings(tmp_path, device=device)
        monkeypatch.setattr("cli_anything.gpt_sovits.core.doctor.shutil.which", lambda name: str(tmp_path / name))
        monkeypatch.setattr("cli_anything.gpt_sovits.core.doctor.status", lambda value: {"running": False})
        stdout = json.dumps({"python": "3.10", "torch": "2.6", "cuda": cuda, "gpu": "test" if cuda else None})
        monkeypatch.setattr("cli_anything.gpt_sovits.core.doctor.subprocess.run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=stdout, stderr=""))
        report = run_doctor(settings)
        assert report["python_gpu"]["ok"] is expected
        assert report["ready"] is expected

    def test_ready_rejects_missing_configured_model(self, tmp_path, monkeypatch):
        settings = self._ready_settings(tmp_path, configured_missing=True)
        monkeypatch.setattr("cli_anything.gpt_sovits.core.doctor.shutil.which", lambda name: str(tmp_path / name))
        monkeypatch.setattr("cli_anything.gpt_sovits.core.doctor.status", lambda value: {"running": False})
        stdout = json.dumps({"python": "3.10", "torch": "2.6", "cuda": True, "gpu": "test"})
        monkeypatch.setattr("cli_anything.gpt_sovits.core.doctor.subprocess.run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=stdout, stderr=""))
        report = run_doctor(settings)
        assert report["configured_models"]["ok"] is False
        assert report["ready"] is False


class TestPackagedSkill:
    def test_canonical_and_packaged_skills_match(self):
        package_root = Path(__file__).resolve().parents[1]
        harness_root = package_root.parents[1]
        project_root = harness_root.parent
        packaged = package_root / "skills" / "SKILL.md"
        canonical = project_root / "skills" / "cli-anything-gpt-sovits" / "SKILL.md"
        assert packaged.read_text(encoding="utf-8") == canonical.read_text(encoding="utf-8")

    def test_skill_uses_real_command_name_and_no_project_fiction(self):
        package_root = Path(__file__).resolve().parents[1]
        content = (package_root / "skills" / "SKILL.md").read_text(encoding="utf-8")
        assert "cli-anything-gpt-sovits" in content
        assert "project new" not in content
        assert "Undo/Redo" not in content
