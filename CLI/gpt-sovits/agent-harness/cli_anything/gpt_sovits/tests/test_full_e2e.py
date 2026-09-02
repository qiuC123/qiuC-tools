from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

from cli_anything.gpt_sovits.core.audio import inspect_wav
from cli_anything.gpt_sovits.core.errors import CLIError
from cli_anything.gpt_sovits.core.phase2b import ALLOWED_UPSTREAM_UNTRACKED
from cli_anything.gpt_sovits.tests.test_core import make_wav
from cli_anything.gpt_sovits.utils.gpt_sovits_backend import synthesize


def _resolve_cli(name):
    """Resolve the installed CLI; release mode forbids module fallback."""
    force = os.environ.get("CLI_ANYTHING_FORCE_INSTALLED", "").strip() == "1"
    path = shutil.which(name)
    if path:
        print(f"[_resolve_cli] Using installed command: {path}")
        return [path]
    if force:
        raise RuntimeError(f"{name} not found in PATH. Install with: pip install -e .")
    print(f"[_resolve_cli] Falling back to: {sys.executable} -m cli_anything.gpt_sovits")
    return [sys.executable, "-m", "cli_anything.gpt_sovits"]


CLI_BASE = _resolve_cli("cli-anything-gpt-sovits")


def _assert_only_allowed_upstream_caches(checkout: Path, status: str) -> None:
    lines = {line.replace("\\", "/") for line in status.splitlines() if line}
    expected_lines = {f"?? {relative}" for relative in ALLOWED_UPSTREAM_UNTRACKED}
    assert lines <= expected_lines, f"unexpected upstream status: {sorted(lines - expected_lines)}"
    for relative, expected in ALLOWED_UPSTREAM_UNTRACKED.items():
        if f"?? {relative}" not in lines:
            continue
        path = checkout / relative
        assert path.stat().st_size == expected["size_bytes"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected["sha256"]


def _subprocess_env() -> dict:
    env = os.environ.copy()
    env.pop("PYTHONUTF8", None)
    env.pop("PYTHONIOENCODING", None)
    return env


class FakeHandler(BaseHTTPRequestHandler):
    response_mode = "wav"
    wav_bytes = b""
    request_started = threading.Event()
    model_switch_requests = []

    def log_message(self, *_args):
        return

    def do_GET(self):
        endpoint = urlparse(self.path).path
        if endpoint == "/openapi.json":
            raw = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if endpoint in {"/set_gpt_weights", "/set_sovits_weights"}:
            type(self).model_switch_requests.append(endpoint)
            raw = b'"success"'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        mode = type(self).response_mode
        type(self).request_started.set()
        if mode == "slow":
            time.sleep(0.3)
        if mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://example.com/escaped")
            self.end_headers()
            return
        if mode == "json":
            raw = json.dumps({"message": "synthetic failure"}).encode()
            self.send_response(400)
            content_type = "application/json"
        elif mode == "wrong_type":
            raw = b"not an audio response"
            self.send_response(200)
            content_type = "text/plain"
        else:
            raw = type(self).wav_bytes
            self.send_response(200)
            content_type = "audio/wav"
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass


@pytest.fixture
def fake_api(tmp_path):
    source = make_wav(tmp_path / "source.wav", seconds=0.2)
    FakeHandler.wav_bytes = source.read_bytes()
    FakeHandler.response_mode = "wav"
    FakeHandler.request_started.clear()
    FakeHandler.model_switch_requests.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class TestHTTPContract:
    def _payload(self, ref: Path) -> dict:
        return {"text": "test", "text_lang": "en", "ref_audio_path": str(ref), "prompt_lang": "en", "prompt_text": "test", "media_type": "wav", "streaming_mode": False}

    def test_success_writes_verified_wav_atomically(self, fake_api, tmp_path):
        ref = make_wav(tmp_path / "ref.wav")
        output = tmp_path / "result.wav"
        result = synthesize(fake_api, self._payload(ref), output, False, 5)
        assert output.read_bytes()[:4] == b"RIFF"
        assert result["output"]["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
        assert not list(tmp_path.glob(".result.wav.*.tmp"))

    def test_http_400_json_is_not_saved(self, fake_api, tmp_path):
        FakeHandler.response_mode = "json"
        output = tmp_path / "error.wav"
        with pytest.raises(CLIError) as raised:
            synthesize(fake_api, self._payload(tmp_path / "ref.wav"), output, False, 5)
        assert raised.value.code == "synthesis_failed"
        assert not output.exists()

    def test_wrong_content_type_is_not_saved(self, fake_api, tmp_path):
        FakeHandler.response_mode = "wrong_type"
        output = tmp_path / "wrong.wav"
        with pytest.raises(CLIError) as raised:
            synthesize(fake_api, self._payload(tmp_path / "ref.wav"), output, False, 5)
        assert raised.value.code == "invalid_api_response"
        assert not output.exists()

    def test_timeout_is_actionable(self, fake_api, tmp_path):
        FakeHandler.response_mode = "slow"
        with pytest.raises(CLIError) as raised:
            synthesize(fake_api, self._payload(tmp_path / "ref.wav"), tmp_path / "slow.wav", False, 0.05)
        assert raised.value.code == "api_unreachable"

    def test_refuses_overwrite(self, fake_api, tmp_path):
        output = tmp_path / "exists.wav"
        output.write_bytes(b"keep")
        with pytest.raises(CLIError) as raised:
            synthesize(fake_api, self._payload(tmp_path / "ref.wav"), output, False, 5)
        assert raised.value.code == "output_exists"
        assert output.read_bytes() == b"keep"

    def test_no_overwrite_race_preserves_concurrent_sentinel(self, fake_api, tmp_path):
        FakeHandler.response_mode = "slow"
        output = tmp_path / "race.wav"
        sentinel = b"created by another process"

        def create_during_request():
            assert FakeHandler.request_started.wait(timeout=2)
            output.write_bytes(sentinel)

        creator = threading.Thread(target=create_during_request)
        creator.start()
        try:
            with pytest.raises(CLIError) as raised:
                synthesize(fake_api, self._payload(tmp_path / "ref.wav"), output, False, 5)
            assert raised.value.code == "output_exists"
            assert output.read_bytes() == sentinel
        finally:
            creator.join(timeout=5)

    def test_redirect_to_remote_is_not_followed(self, fake_api, tmp_path):
        FakeHandler.response_mode = "redirect"
        output = tmp_path / "redirect.wav"
        with pytest.raises(CLIError) as raised:
            synthesize(fake_api, self._payload(tmp_path / "ref.wav"), output, False, 5)
        assert raised.value.details["status"] == 302
        assert not output.exists()


class TestInstalledCommand:
    def _run(self, args, timeout=120):
        return subprocess.run(CLI_BASE + args, capture_output=True, text=True, encoding="utf-8", env=_subprocess_env(), timeout=timeout)

    def test_help_from_unrelated_directory(self):
        result = self._run(["--help"])
        assert result.returncode == 0, result.stderr
        assert "synthesize" in result.stdout

    def test_default_repl_starts_and_exits(self):
        result = subprocess.run(CLI_BASE, input="quit\n", capture_output=True, text=True, encoding="utf-8", env=_subprocess_env(), timeout=30)
        assert result.returncode == 0, result.stderr
        assert "cli-anything" in result.stdout and "Goodbye" in result.stdout
        assert "Active skill:" in result.stdout

    def test_doctor_json_from_unrelated_directory(self):
        result = self._run(["doctor", "--json"])
        assert result.returncode == 0, result.stderr
        body = json.loads(result.stdout)
        assert body["command"] == "doctor"
        assert body["data"]["checkout"]["ok"] is True

    def test_synthesize_dry_run_from_unrelated_directory(self, tmp_path):
        ref = make_wav(tmp_path / "reference.wav")
        output = tmp_path / "dry-run.wav"
        result = self._run(["synthesize", "--text", "test", "--text-lang", "en", "--ref-audio", str(ref), "--prompt-lang", "en", "--output", str(output), "--dry-run", "--json"])
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["data"]["dry_run"] is True
        assert not output.exists()

    def test_fake_api_full_cli_output(self, fake_api, tmp_path):
        ref = make_wav(tmp_path / "reference.wav")
        output = tmp_path / "cli-output.wav"
        result = self._run(["--api-url", fake_api, "synthesize", "--text", "test", "--text-lang", "en", "--ref-audio", str(ref), "--prompt-lang", "en", "--output", str(output), "--json"])
        assert result.returncode == 0, result.stderr
        body = json.loads(result.stdout)
        assert body["ok"] is True
        assert body["data"]["output"]["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()

    @pytest.mark.parametrize("command,suffix", [("use-gpt", ".ckpt"), ("use-sovits", ".pth")])
    def test_external_fake_api_model_switch_is_refused(self, fake_api, tmp_path, command, suffix):
        weight = tmp_path / f"model{suffix}"
        weight.write_bytes(b"synthetic test weight marker")
        result = self._run(["--api-url", fake_api, "model", command, str(weight), "--json"])
        assert result.returncode != 0
        body = json.loads(result.stdout)
        assert body["error"]["code"] == "managed_service_required"
        assert FakeHandler.model_switch_requests == []

    def test_external_fake_api_model_switch_dry_run_is_allowed_without_request(self, fake_api, tmp_path):
        weight = tmp_path / "model.ckpt"
        weight.write_bytes(b"synthetic test weight marker")
        result = self._run(["--api-url", fake_api, "model", "use-gpt", str(weight), "--dry-run", "--json"])
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["data"]["dry_run"] is True
        assert FakeHandler.model_switch_requests == []

    @pytest.mark.parametrize(
        "args",
        [
            ["--json", "synthesize"],
            ["synthesize", "--json"],
            ["synthesize", "--top-p", "0", "--json"],
            ["unknown-command", "--json"],
            ["--api-url", "http://example.com:9880", "doctor", "--json"],
        ],
    )
    def test_usage_and_root_config_errors_are_json_envelopes(self, args):
        result = self._run(args)
        assert result.returncode != 0
        body = json.loads(result.stdout)
        assert set(body) == {"ok", "command", "data", "warnings", "error"}
        assert body["ok"] is False and body["error"]["code"]

    def test_utf8_help_and_json_without_utf8_environment_override(self, tmp_path):
        env = _subprocess_env()
        help_result = subprocess.run(CLI_BASE + ["--help"], capture_output=True, env=env, timeout=30)
        help_text = help_result.stdout.decode("utf-8")
        assert help_result.returncode == 0 and "语音合成" in help_text
        ref = make_wav(tmp_path / "中文 参考.wav")
        json_result = subprocess.run(CLI_BASE + ["reference", "inspect", str(ref), "--json"], capture_output=True, env=env, timeout=30)
        body = json.loads(json_result.stdout.decode("utf-8"))
        assert json_result.returncode == 0 and "中文 参考.wav" in body["data"]["path"]

    def test_repl_executes_command_with_spaced_chinese_path(self, tmp_path):
        ref = make_wav(tmp_path / "中文 参考.wav")
        script = f'reference inspect "{ref}" --json\nquit\n'
        result = subprocess.run(CLI_BASE, input=script, capture_output=True, text=True, encoding="utf-8", env=_subprocess_env(), timeout=30)
        assert result.returncode == 0, result.stderr
        escaped_path = json.dumps(str(ref.resolve()), ensure_ascii=False)[1:-1]
        assert escaped_path in result.stdout
        assert '"ok": true' in result.stdout
        assert hashlib.sha256(ref.read_bytes()).hexdigest() in result.stdout

    @pytest.mark.parametrize("command", ["synthesize --json", "synthesize --top-p 0 --json"])
    def test_repl_json_usage_errors_use_stable_envelope(self, command):
        result = subprocess.run(CLI_BASE, input=f"{command}\nquit\n", capture_output=True, text=True, encoding="utf-8", env=_subprocess_env(), timeout=30)
        assert result.returncode == 0, result.stderr
        assert '"ok": false' in result.stdout
        assert '"command": "usage"' in result.stdout
        assert '"code": "usage_error"' in result.stdout
        assert "✗" not in result.stdout

    def test_repl_non_json_usage_error_stays_human_friendly(self):
        result = subprocess.run(CLI_BASE, input="synthesize\nquit\n", capture_output=True, text=True, encoding="utf-8", env=_subprocess_env(), timeout=30)
        assert result.returncode == 0, result.stderr
        human_output = result.stdout + result.stderr
        assert "✗" in human_output
        assert "Missing option" in human_output

    def test_phase2a_training_doctor_installed_json(self):
        result = self._run(["training", "doctor", "--json"])
        assert result.returncode == 0, result.stdout + result.stderr
        data = json.loads(result.stdout)["data"]
        assert {"ready", "training_scripts", "python_gpu", "ffmpeg", "disk", "offline_asr", "uvr5", "missing"} <= set(data)

    def test_phase2a_installed_dataset_workflow(self, tmp_path):
        source = make_wav(tmp_path / "中文 来源.wav", seconds=4, rate=44100)
        output = tmp_path / "中文 候选.wav"
        extracted = self._run(["dataset", "extract", "--source", str(source), "--start", "1", "--end", "3", "--output", str(output), "--json"])
        assert extracted.returncode == 0, extracted.stdout + extracted.stderr
        report = json.loads(extracted.stdout)["data"]["inspection"]
        assert report["compliant"] is True
        inspected = self._run(["dataset", "inspect", str(output), "--json"])
        assert inspected.returncode == 0, inspected.stdout + inspected.stderr
        record = {
            "audio_path": str(output.resolve()),
            "source_path": str(source.resolve()),
            "start": "00:00:01.000",
            "end": "00:00:03.000",
            "duration_seconds": 2.0,
            "sha256": report["sha256"],
            "processing": "original",
            "text_ja": "インストール済みコマンドの試験です",
            "asr_source": "test-fixture",
            "review_status": "pending",
        }
        records = tmp_path / "records.json"
        records.write_text(json.dumps([record], ensure_ascii=False), encoding="utf-8")
        manifest = tmp_path / "manifest.jsonl"
        manifested = self._run(["dataset", "manifest", "--records", str(records), "--output", str(manifest), "--json"])
        assert manifested.returncode == 0, manifested.stdout + manifested.stderr
        assert json.loads(manifest.read_text(encoding="utf-8"))["review_status"] == "pending"

    def test_phase2b_installed_plan_and_status_workflow(self, tmp_path):
        data_dir = tmp_path / "阶段2A 数据"
        audio_dir = data_dir / "original"
        audio_dir.mkdir(parents=True)
        rows = []
        for index in range(3):
            audio = audio_dir / f"speaker-{index + 1:03d}.wav"
            audio.write_bytes(f"installed-phase2b-{index}".encode())
            rows.append(
                {
                    "audio_path": str(audio.resolve()),
                    "sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
                    "processing": "original",
                    "text_ja": f"安装态試験 {index + 1}",
                    "review_status": "approved",
                }
            )
        rejected = audio_dir / "rejected.wav"
        rejected.write_bytes(b"must-not-enter-labels")
        rows.append(
            {
                "audio_path": str(rejected.resolve()),
                "sha256": hashlib.sha256(rejected.read_bytes()).hexdigest(),
                "processing": "original",
                "text_ja": "不使用",
                "review_status": "rejected",
            }
        )
        manifest = data_dir / "manifest.jsonl"
        manifest.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
        workspace = data_dir / "阶段2B 训练工作区"
        planned = self._run(
            [
                "training",
                "plan",
                "--manifest",
                str(manifest),
                "--workspace",
                str(workspace),
                "--expected-manifest-sha256",
                manifest_sha,
                "--speaker",
                "narrator",
                "--language",
                "ja",
                "--expected-approved-count",
                "3",
                "--json",
            ]
        )
        assert planned.returncode == 0, planned.stdout + planned.stderr
        body = json.loads(planned.stdout)
        assert body["data"]["approved_count"] == 3
        labels = (workspace / "training.list").read_text(encoding="utf-8").splitlines()
        assert len(labels) == 3
        assert all("|narrator|ja|" in line and "rejected.wav" not in line for line in labels)
        status = self._run(["training", "status", "--workspace", str(workspace), "--json"])
        assert status.returncode == 0, status.stdout + status.stderr
        status_body = json.loads(status.stdout)
        assert status_body["data"]["approved_count"] == 3
        assert status_body["data"]["stages"]["preprocess"]["status"] == "not_started"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _required_real_checkout() -> Path:
    checkout_value = os.environ.get("GPT_SOVITS_TEST_CHECKOUT") or os.environ.get("GPT_SOVITS_CHECKOUT")
    if not checkout_value:
        pytest.fail(
            "真实 E2E 需要设置 GPT_SOVITS_TEST_CHECKOUT 或 GPT_SOVITS_CHECKOUT；"
            "缺失时不允许跳过或使用伪后端"
        )
    return Path(checkout_value).expanduser().resolve()


def _make_system_tts_reference(path: Path, secret_marker: str) -> str:
    prompt = f"系统合成测试。{secret_marker}。"
    escaped_path = str(path).replace("'", "''")
    escaped_prompt = prompt.replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$voice = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$voice.SelectVoice('Microsoft Huihui Desktop'); "
        "$voice.Rate = -1; "
        f"$voice.SetOutputToWaveFile('{escaped_path}'); "
        f"$voice.Speak('{escaped_prompt}'); "
        "$voice.Dispose()"
    )
    proc = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"Windows System.Speech failed: {proc.stderr}")
    report = inspect_wav(path, require_non_silent=True)
    if not 3.0 <= report["duration_seconds"] <= 10.0:
        raise RuntimeError(f"System TTS reference duration must be 3-10 seconds: {report}")
    return prompt


class TestRealGPTSoVITS:
    def test_real_checkout_environment_is_required(self, monkeypatch):
        monkeypatch.delenv("GPT_SOVITS_TEST_CHECKOUT", raising=False)
        monkeypatch.delenv("GPT_SOVITS_CHECKOUT", raising=False)
        with pytest.raises(pytest.fail.Exception, match="GPT_SOVITS_TEST_CHECKOUT"):
            _required_real_checkout()

    def test_test_checkout_environment_has_precedence(self, tmp_path, monkeypatch):
        generic_checkout = tmp_path / "generic"
        test_checkout = tmp_path / "test"
        monkeypatch.setenv("GPT_SOVITS_CHECKOUT", str(generic_checkout))
        monkeypatch.setenv("GPT_SOVITS_TEST_CHECKOUT", str(test_checkout))
        assert _required_real_checkout() == test_checkout.resolve()

    def test_real_backend_generates_valid_wav(self):
        checkout = _required_real_checkout()
        default_runtime = Path(os.environ.get("LOCALAPPDATA", "")) / "qiuC" / "gpt-sovits-cli" / "e2e-runtime" / "Scripts" / "python.exe"
        runtime = Path(os.environ.get("GPT_SOVITS_E2E_RUNTIME", default_runtime))
        if not checkout.is_dir() or not runtime.is_file():
            pytest.fail("真实 GPT-SoVITS 后端或隔离 E2E 运行时缺失；真实 E2E 不允许跳过")
        if runtime.resolve() == (checkout / ".conda" / "python.exe").resolve():
            pytest.fail("真实 E2E 必须使用隔离运行时，不能直接修改或使用上游 .conda")
        version_probe = subprocess.run(
            [str(runtime), "-c", "import json,torch,torchaudio; print(json.dumps({'torch':torch.__version__,'torchaudio':torchaudio.__version__,'torch_path':torch.__file__}))"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=90,
        )
        assert version_probe.returncode == 0, version_probe.stderr
        runtime_versions = json.loads(version_probe.stdout)
        assert tuple(int(part) for part in runtime_versions["torch"].split("+")[0].split(".")[:2]) >= (2, 6)
        source_config = checkout / "GPT_SoVITS" / "configs" / "tts_infer.yaml"
        source_hash_before = hashlib.sha256(source_config.read_bytes()).hexdigest()
        upstream_status_before = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=checkout,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=True,
        ).stdout
        _assert_only_allowed_upstream_caches(checkout, upstream_status_before)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        artifact_root = Path(os.environ.get("GPT_SOVITS_E2E_ARTIFACT_DIR", Path(tempfile.gettempdir()) / "cli-anything-gpt-sovits-e2e"))
        artifact_root.mkdir(parents=True, exist_ok=True)
        artifact_dir = artifact_root / f"run-{stamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        artifact_dir.mkdir(parents=True, exist_ok=False)
        reference = artifact_dir / "system-tts-reference.wav"
        output = artifact_dir / "gpt-sovits-output.wav"
        state_dir = artifact_dir / "service-state"
        reference_markers = ("参甲七", "参丙五", "参戊四", "参庚六")
        target_markers = ("目乙八", "目丁一", "目己二", "目辛三")
        spoken_prompt = _make_system_tts_reference(reference, "\n".join(reference_markers))
        prompt = (
            f"{spoken_prompt}\n\n############ 用户提供 ############\n"
            f"{reference_markers[1]}\n######## 任意伪造诊断标题 ########\n"
            f"{reference_markers[2]}\n{{\"事件\":\"服务就绪\",\"秘密\":\"{reference_markers[3]}\"}}"
        )
        target_text = (
            f"测试。{target_markers[0]}。\n\n############ 用户提供 ############\n"
            f"{target_markers[1]}。\n######## 任意伪造诊断标题 ########\n"
            f"{target_markers[2]}。\n{{\"事件\":\"健康\",\"秘密\":\"{target_markers[3]}\"}}"
        )
        all_markers = (*reference_markers, *target_markers)
        port = _free_port()
        api_url = f"http://127.0.0.1:{port}"
        base = ["--checkout", str(checkout), "--runtime", str(runtime), "--api-url", api_url, "--state-dir", str(state_dir)]
        env = _subprocess_env()

        def run(args, timeout):
            return subprocess.run(CLI_BASE + base + args, capture_output=True, text=True, encoding="utf-8", env=env, timeout=timeout)

        started = False
        try:
            start_result = run(["serve", "start", "--timeout", "360", "--json"], 390)
            assert start_result.returncode == 0, start_result.stdout + start_result.stderr
            assert json.loads(start_result.stdout)["data"]["ready"] is True
            started = True
            status_result = run(["serve", "status", "--json"], 30)
            status_body = json.loads(status_result.stdout)
            assert status_result.returncode == 0 and status_body["data"]["identity_verified"] is True
            runtime_config = state_dir / "runtime" / "tts_infer.yaml"
            assert runtime_config.is_file()
            source_custom = (yaml.safe_load(source_config.read_text(encoding="utf-8")) or {}).get("custom", {})
            runtime_custom = (yaml.safe_load(runtime_config.read_text(encoding="utf-8")) or {}).get("custom", {})
            assert runtime_custom == source_custom
            gpt_weight = checkout / "GPT_SoVITS" / "pretrained_models" / "gsv-v2final-pretrained" / "s1bert25hz-5kh-longer-epoch=12-step=369668.ckpt"
            sovits_weight = checkout / "GPT_SoVITS" / "pretrained_models" / "gsv-v2final-pretrained" / "s2G2333k.pth"
            gpt_result = run(["model", "use-gpt", str(gpt_weight), "--timeout", "180", "--json"], 210)
            assert gpt_result.returncode == 0, gpt_result.stdout + gpt_result.stderr
            assert json.loads(gpt_result.stdout)["data"]["backend"]["status"] == 200
            sovits_result = run(["model", "use-sovits", str(sovits_weight), "--timeout", "180", "--json"], 210)
            assert sovits_result.returncode == 0, sovits_result.stdout + sovits_result.stderr
            assert json.loads(sovits_result.stdout)["data"]["backend"]["status"] == 200
            synthesis_result = run(
                [
                    "synthesize", "--text", target_text, "--text-lang", "zh",
                    "--ref-audio", str(reference), "--prompt-lang", "zh", "--prompt-text", prompt,
                    "--output", str(output), "--seed", "12345", "--json",
                ],
                660,
            )
            assert synthesis_result.returncode == 0, synthesis_result.stdout + synthesis_result.stderr
            body = json.loads(synthesis_result.stdout)
            report = inspect_wav(output, require_non_silent=True)
            assert output.read_bytes()[:12][0:4] == b"RIFF"
            assert output.read_bytes()[8:12] == b"WAVE"
            assert report["duration_seconds"] > 0
            assert report["channels"] == 1
            assert report["sample_rate"] > 0
            assert report["rms"] > 10
            assert report["ffprobe"]["format"]["format_name"]
            assert body["data"]["output"]["sha256"] == report["sha256"]
            assert body["data"]["output"]["size_bytes"] == output.stat().st_size
            logs_result = run(["serve", "logs", "--lines", "400", "--json"], 30)
            assert logs_result.returncode == 0, logs_result.stdout + logs_result.stderr
            log_lines = json.loads(logs_result.stdout)["data"]["lines"]
            served_log = "\n".join(log_lines)
            raw_log = (state_dir / "api.log").read_text(encoding="utf-8", errors="strict")
            for marker in all_markers:
                assert marker not in raw_log
                assert marker not in served_log
            artifact_bytes = b"".join(path.read_bytes() for path in artifact_dir.rglob("*") if path.is_file())
            assert all(marker.encode("utf-8") not in artifact_bytes for marker in all_markers)
            assert "�" not in served_log
            lifecycle_events = [json.loads(line)["event"] for line in raw_log.splitlines()]
            assert {"start_requested", "spawned", "service_ready", "health"} <= set(lifecycle_events)
            assert all(json.loads(line)["event"] for line in log_lines)
            assert '"output_policy":"discard"' in raw_log
            assert "Uvicorn running" not in raw_log
            print(f"\n  REAL_WAV: {output}")
            print(f"  REAL_LOG: {state_dir / 'api.log'}")
            print(f"  REAL_REPORT: {json.dumps(report, ensure_ascii=False)}")
            print(f"  E2E_RUNTIME: {runtime}")
            print(f"  E2E_RUNTIME_VERSIONS: {json.dumps(runtime_versions)}")
        finally:
            if started:
                stop_result = run(["serve", "stop", "--json"], 60)
                assert stop_result.returncode == 0, stop_result.stdout + stop_result.stderr
                stopped_log = (state_dir / "api.log").read_text(encoding="utf-8", errors="strict")
                assert '"event":"service_stopped"' in stopped_log
                artifact_bytes = b"".join(path.read_bytes() for path in artifact_dir.rglob("*") if path.is_file())
                assert all(marker.encode("utf-8") not in artifact_bytes for marker in all_markers)
            source_hash_after = hashlib.sha256(source_config.read_bytes()).hexdigest()
            upstream_status_after = subprocess.run(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                cwd=checkout,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                check=True,
            ).stdout
            print(f"  UPSTREAM_CONFIG_SHA256_BEFORE: {source_hash_before}")
            print(f"  UPSTREAM_CONFIG_SHA256_AFTER: {source_hash_after}")
            assert source_hash_after == source_hash_before
            _assert_only_allowed_upstream_caches(checkout, upstream_status_after)
            assert upstream_status_after == upstream_status_before
            print(f"  ARTIFACT_DIR: {artifact_dir}")
