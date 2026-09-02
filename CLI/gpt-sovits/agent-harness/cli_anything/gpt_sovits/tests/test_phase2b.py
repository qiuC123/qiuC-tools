from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from cli_anything.gpt_sovits.core.config import Settings
from cli_anything.gpt_sovits.core.errors import CLIError
from cli_anything.gpt_sovits.core.phase2b import (
    prepare_training_workspace,
    run_preprocessing,
    run_trial_training,
    training_workspace_status,
)
from cli_anything.gpt_sovits.gpt_sovits_cli import cli


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _settings(tmp_path: Path) -> Settings:
    checkout = tmp_path / "checkout"
    configs = checkout / "GPT_SoVITS" / "configs"
    configs.mkdir(parents=True)
    (checkout / "GPT_SoVITS" / "prepare_datasets").mkdir()
    for script in ("1-get-text.py", "2-get-hubert-wav32k.py", "2-get-sv.py", "3-get-semantic.py"):
        (checkout / "GPT_SoVITS" / "prepare_datasets" / script).write_text("pass\n", encoding="utf-8")
    for script in ("s1_train.py", "s2_train.py"):
        (checkout / "GPT_SoVITS" / script).write_text("pass\n", encoding="utf-8")
    (configs / "s2v2ProPlus.json").write_text(
        json.dumps({"train": {}, "data": {}, "model": {}, "content_module": "cnhubert"}), encoding="utf-8"
    )
    (configs / "s1longer-v2.yaml").write_text(
        yaml.safe_dump({"train": {}, "data": {}, "model": {}, "inference": {}}), encoding="utf-8"
    )
    pretrained = checkout / "GPT_SoVITS" / "pretrained_models"
    (pretrained / "v2Pro").mkdir(parents=True)
    for relative in ("s1v3.ckpt", "v2Pro/s2Gv2ProPlus.pth", "v2Pro/s2Dv2ProPlus.pth"):
        path = pretrained / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        _torch_zip(path)
    sv_model = pretrained / "sv" / "pretrained_eres2netv2w24s4ep4.ckpt"
    sv_model.parent.mkdir(parents=True)
    sv_model.write_bytes(b"speaker-vector-model")
    for relative in ("chinese-roberta-wwm-ext-large", "chinese-hubert-base"):
        (pretrained / relative).mkdir(parents=True)
    runtime = tmp_path / "python.exe"
    runtime.write_bytes(b"runtime")
    tts = configs / "tts_infer.yaml"
    tts.write_text("x: 1\n", encoding="utf-8")
    return Settings(checkout, runtime, "http://127.0.0.1:9880", tts, tmp_path / "state")


def _manifest(tmp_path: Path, *, approved: int = 3, processing: str = "original", language: str = "ja") -> Path:
    audio_dir = tmp_path / "data" / "original"
    audio_dir.mkdir(parents=True)
    rows = []
    for index in range(approved):
        audio = audio_dir / f"speaker-{index + 1:03d}.wav"
        audio.write_bytes(f"audio-{index}".encode())
        rows.append(
            {
                "audio_path": str(audio.resolve()),
                "sha256": _sha(audio),
                "processing": processing,
                f"text_{language}": f"training text {index + 1}",
                "review_status": "approved",
            }
        )
    rejected = audio_dir / "rejected.wav"
    rejected.write_bytes(b"rejected")
    rows.append(
        {
            "audio_path": str(rejected.resolve()),
            "sha256": _sha(rejected),
            "processing": "original",
            f"text_{language}": "unused",
            "review_status": "rejected",
        }
    )
    manifest = tmp_path / "data" / "manifest.jsonl"
    manifest.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    return manifest


def _prepare(tmp_path: Path):
    settings = _settings(tmp_path)
    manifest = _manifest(tmp_path)
    workspace = tmp_path / "data" / "阶段2B_训练工作区"
    result = prepare_training_workspace(settings, manifest, workspace, _sha(manifest))
    return settings, manifest, workspace, result


def _torch_zip(path: Path, *, modern: bool = True) -> None:
    """Write a non-executable PyTorch ZIP shape for boundary tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("archive/data.pkl", b"metadata-only-test-fixture")
        archive.writestr("archive/version", b"3\n")
        if modern:
            archive.writestr("archive/byteorder", b"little")
            archive.writestr("archive/.data/serialization_id", b"fixture")
        archive.writestr("archive/data/0", b"tensor-bytes")


def _gpt_metadata(path: Path) -> dict:
    match = __import__("re").fullmatch(r"epoch=(\d+)-step=(\d+)\.ckpt", path.name)
    return {
        "format": "pytorch-zip",
        "top_level_keys": ["state_dict", "optimizer_states", "loops", "callbacks", "lr_schedulers"],
        "epoch": int(match.group(1)) if match else 9,
        "global_step": int(match.group(2)) if match else 210,
        "state_dict_entries": 2,
        "optimizer_state_entries": 1,
    }


def _complete_sovits(workspace: Path, *, speaker: str = "speaker") -> None:
    fixed = workspace / "features" / "logs_s2_v2ProPlus"
    for epoch in range(1, 9):
        step = epoch * 87
        _torch_zip(fixed / f"G_{step}.pth")
        _torch_zip(fixed / f"D_{step}.pth")
        _torch_zip(workspace / "checkpoints" / "sovits" / f"{speaker}_e{epoch}_s{step}.pth")


def _complete_gpt(workspace: Path, *, speaker: str = "speaker") -> None:
    for epoch in range(10):
        _torch_zip(workspace / "internal" / "gpt" / "ckpt" / f"epoch={epoch}-step={(epoch + 1) * 21}.ckpt")
        _torch_zip(workspace / "checkpoints" / "gpt" / f"{speaker}-e{epoch + 1}.ckpt")


class TestTrainingPlan:
    def test_plan_accepts_explicit_neutral_identity_language_and_count(self, tmp_path):
        settings = _settings(tmp_path)
        manifest = _manifest(tmp_path, approved=3, language="en")
        workspace = tmp_path / "data" / "work"

        result = prepare_training_workspace(
            settings,
            manifest,
            workspace,
            _sha(manifest),
            speaker="narrator",
            language="en",
            expected_approved_count=3,
        )

        plan = json.loads((workspace / "plan.json").read_text(encoding="utf-8"))
        labels = (workspace / "training.list").read_text(encoding="utf-8").splitlines()
        assert result["approved_count"] == 3
        assert plan["speaker"] == "narrator"
        assert plan["language"] == "en"
        assert plan["expected_approved_count"] == 3
        assert all("|narrator|en|" in line for line in labels)
        assert json.loads((workspace / "configs" / "s2.json").read_text(encoding="ascii"))["name"] == "narrator"
        assert yaml.safe_load((workspace / "configs" / "s1.yaml").read_text(encoding="ascii"))["train"]["exp_name"] == "narrator"

    def test_approved_only_plan_uses_manifest_count_and_neutral_default(self, tmp_path):
        _, _, workspace, result = _prepare(tmp_path)
        assert result["approved_count"] == 3
        lines = (workspace / "training.list").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        assert all("|speaker|ja|" in line for line in lines)
        assert all("rejected.wav" not in line for line in lines)

    @pytest.mark.parametrize("approved", [2, 4])
    def test_explicit_wrong_approved_count_is_rejected(self, tmp_path, approved):
        settings = _settings(tmp_path)
        manifest = _manifest(tmp_path, approved=approved)
        with pytest.raises(CLIError, match="3"):
            prepare_training_workspace(
                settings,
                manifest,
                tmp_path / "data" / "work",
                _sha(manifest),
                expected_approved_count=3,
            )

    def test_approved_uvr_variant_is_rejected(self, tmp_path):
        settings = _settings(tmp_path)
        manifest = _manifest(tmp_path, processing="uvr5")
        with pytest.raises(CLIError, match="original"):
            prepare_training_workspace(settings, manifest, tmp_path / "data" / "work", _sha(manifest))

    @pytest.mark.parametrize("bad_text", ["a|b", "a\nb", "a\rb", "a\0b", ""])
    def test_unsafe_or_empty_text_is_rejected(self, tmp_path, bad_text):
        settings = _settings(tmp_path)
        manifest = _manifest(tmp_path)
        rows = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines()]
        rows[0]["text_ja"] = bad_text
        manifest.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        with pytest.raises(CLIError):
            prepare_training_workspace(settings, manifest, tmp_path / "data" / "work", _sha(manifest))

    def test_audio_hash_change_is_rejected(self, tmp_path):
        settings = _settings(tmp_path)
        manifest = _manifest(tmp_path)
        first = Path(json.loads(manifest.read_text(encoding="utf-8").splitlines()[0])["audio_path"])
        first.write_bytes(b"changed")
        with pytest.raises(CLIError, match="SHA-256"):
            prepare_training_workspace(settings, manifest, tmp_path / "data" / "work", _sha(manifest))

    def test_manifest_hash_mismatch_is_rejected(self, tmp_path):
        settings = _settings(tmp_path)
        manifest = _manifest(tmp_path)
        with pytest.raises(CLIError, match="manifest"):
            prepare_training_workspace(settings, manifest, tmp_path / "data" / "work", "0" * 64)

    def test_dry_run_has_no_side_effects(self, tmp_path):
        settings = _settings(tmp_path)
        manifest = _manifest(tmp_path)
        workspace = tmp_path / "data" / "work"
        result = prepare_training_workspace(settings, manifest, workspace, _sha(manifest), dry_run=True)
        assert result["dry_run"] is True
        assert not workspace.exists()

    def test_default_refuses_overwrite(self, tmp_path):
        settings, manifest, workspace, _ = _prepare(tmp_path)
        with pytest.raises(CLIError, match="存在"):
            prepare_training_workspace(settings, manifest, workspace, _sha(manifest))

    def test_workspace_must_be_sibling_under_dataset_dir(self, tmp_path):
        settings = _settings(tmp_path)
        manifest = _manifest(tmp_path)
        with pytest.raises(CLIError):
            prepare_training_workspace(settings, manifest, tmp_path / "elsewhere", _sha(manifest))

    def test_template_configs_are_copied_without_mutating_source(self, tmp_path):
        settings = _settings(tmp_path)
        manifest = _manifest(tmp_path)
        s1 = settings.checkout / "GPT_SoVITS" / "configs" / "s1longer-v2.yaml"
        s2 = settings.checkout / "GPT_SoVITS" / "configs" / "s2v2ProPlus.json"
        before = (_sha(s1), _sha(s2))
        workspace = tmp_path / "data" / "work"
        prepare_training_workspace(settings, manifest, workspace, _sha(manifest))
        assert (_sha(s1), _sha(s2)) == before
        copied_s2 = json.loads((workspace / "configs" / "s2.json").read_text(encoding="utf-8"))
        preprocess_s2_path = workspace / "configs" / "s2-preprocess.json"
        preprocess_s2 = json.loads(preprocess_s2_path.read_text(encoding="ascii"))
        copied_s1 = yaml.safe_load((workspace / "configs" / "s1.yaml").read_text(encoding="utf-8"))
        assert copied_s2["train"]["batch_size"] == 1
        assert copied_s2["train"]["epochs"] == 8
        assert copied_s2["train"]["save_every_epoch"] == 1
        assert copied_s2["train"]["fp16_run"] is True
        assert copied_s2["model"]["version"] == "v2ProPlus"
        assert "version" not in preprocess_s2["model"]
        assert preprocess_s2_path.read_bytes().decode("ascii")
        assert {key: value for key, value in copied_s2["model"].items() if key != "version"} == preprocess_s2["model"]
        assert copied_s1["train"]["batch_size"] == 1
        assert copied_s1["train"]["epochs"] == 10
        assert copied_s1["train"]["save_every_n_epoch"] == 1
        assert copied_s1["train"]["precision"] == "16-mixed"
        assert copied_s2["data"]["exp_dir"] == str(workspace / "features")
        assert copied_s1["train_semantic_path"] == str(workspace / "features" / "6-name2semantic.tsv")
        assert ".stage-" not in json.dumps(copied_s2)
        assert ".stage-" not in yaml.safe_dump(copied_s1)
        assert (workspace / "configs" / "s2.json").read_bytes().decode("ascii")
        assert (workspace / "configs" / "s1.yaml").read_bytes().decode("ascii")


class TestPhase2BSecurityBoundaries:
    def test_plan_records_hashes_for_every_isolated_execution_artifact(self, tmp_path):
        _, _, workspace, _ = _prepare(tmp_path)
        plan = json.loads((workspace / "plan.json").read_text(encoding="utf-8"))
        assert plan["schema"] == "cli-anything-gpt-sovits/phase2b-plan-v2"
        assert set(plan["artifacts"]) == {
            "approved_manifest",
            "labels",
            "s1_config",
            "s2_config",
            "s2_preprocess_config",
        }
        for artifact in plan["artifacts"].values():
            path = Path(artifact["path"])
            assert path.is_file()
            assert artifact["sha256"] == _sha(path)

    def test_plan_binds_all_approved_pretrained_weights(self, tmp_path):
        _, _, workspace, _ = _prepare(tmp_path)
        plan = json.loads((workspace / "plan.json").read_text(encoding="utf-8"))
        assert set(plan["pretrained_models"]) == {"gpt", "sovits_generator", "sovits_discriminator"}
        for item in plan["pretrained_models"].values():
            path = Path(item["path"])
            assert item["sha256"] == _sha(path)
            assert item["size_bytes"] == path.stat().st_size
            assert item["structure"]["format"] == "pytorch-zip"

    def test_approved_legacy_pytorch_zip_without_optional_modern_metadata_is_accepted(self, tmp_path):
        settings = _settings(tmp_path)
        _torch_zip(settings.checkout / "GPT_SoVITS/pretrained_models/s1v3.ckpt", modern=False)
        manifest = _manifest(tmp_path)
        result = prepare_training_workspace(
            settings,
            manifest,
            tmp_path / "data" / "work",
            _sha(manifest),
        )
        assert result["approved_count"] == 3

    @pytest.mark.parametrize("relative", ["configs/s1.yaml", "configs/s2.json", "training.list"])
    def test_execution_rejects_isolated_artifact_tamper_before_runner(self, tmp_path, relative):
        settings, _, workspace, _ = _prepare(tmp_path)
        path = workspace / relative
        path.write_bytes(path.read_bytes() + b"\ntampered")
        calls = []
        with pytest.raises(CLIError, match="哈希|篡改"):
            run_preprocessing(
                settings,
                workspace,
                process_runner=lambda *args, **kwargs: calls.append(args),
                upstream_status_reader=lambda: [],
            )
        assert calls == []

    def test_execution_rejects_output_path_escape_even_when_plan_hash_is_rewritten(self, tmp_path):
        settings, _, workspace, _ = _prepare(tmp_path)
        config_path = workspace / "configs" / "s1.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="ascii"))
        config["output_dir"] = str(tmp_path / "escaped")
        config_path.write_text(yaml.safe_dump(config, allow_unicode=False, sort_keys=False), encoding="ascii")
        plan_path = workspace / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["artifacts"]["s1_config"]["sha256"] = _sha(config_path)
        plan["artifacts"]["s1_config"]["size_bytes"] = config_path.stat().st_size
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(CLIError, match="输出路径|工作区"):
            run_trial_training(settings, workspace, "gpt", dry_run=True)

    def test_execution_rejects_pretrained_path_change_even_when_artifact_hash_is_rewritten(self, tmp_path):
        settings, _, workspace, _ = _prepare(tmp_path)
        config_path = workspace / "configs" / "s1.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="ascii"))
        outside = tmp_path / "outside" / "s1v3.ckpt"
        _torch_zip(outside)
        config["pretrained_s1"] = str(outside)
        config_path.write_text(yaml.safe_dump(config, allow_unicode=False, sort_keys=False), encoding="ascii")
        plan_path = workspace / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["artifacts"]["s1_config"].update({"sha256": _sha(config_path), "size_bytes": config_path.stat().st_size})
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(CLIError, match="预训练|批准|路径|上游"):
            run_trial_training(settings, workspace, "gpt", dry_run=True)

    def test_execution_rejects_replaced_pretrained_weight(self, tmp_path):
        settings, _, workspace, _ = _prepare(tmp_path)
        pretrained = settings.checkout / "GPT_SoVITS/pretrained_models/s1v3.ckpt"
        with zipfile.ZipFile(pretrained, "a") as archive:
            archive.writestr("archive/replacement", b"changed")
        with pytest.raises(CLIError, match="预训练|哈希|替换"):
            run_trial_training(settings, workspace, "gpt", dry_run=True)

    def test_overwrite_rejects_unknown_files_and_preserves_directory(self, tmp_path):
        settings, manifest, workspace, _ = _prepare(tmp_path)
        unknown = workspace / "user-note.txt"
        unknown.write_text("keep me", encoding="utf-8")
        with pytest.raises(CLIError, match="覆盖|白名单|未知"):
            prepare_training_workspace(settings, manifest, workspace, _sha(manifest), overwrite=True)
        assert unknown.read_text(encoding="utf-8") == "keep me"
        assert workspace.is_dir()

    def test_arbitrary_checkpoint_bytes_never_enable_unsafe_torch_load(self, tmp_path):
        settings, _, workspace, _ = _prepare(tmp_path)
        (workspace / "preprocess.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
        checkpoint = workspace / "internal" / "gpt" / "ckpt" / "epoch=1-step=42.ckpt"
        checkpoint.parent.mkdir(parents=True)
        checkpoint.write_bytes(b"arbitrary bytes")
        calls = []
        with pytest.raises(CLIError, match="检查点|结构|信任"):
            run_trial_training(
                settings,
                workspace,
                "gpt",
                process_runner=lambda *args, **kwargs: calls.append(kwargs["env"]),
                upstream_status_reader=lambda: [],
                checkpoint_inspector=_gpt_metadata,
            )
        assert calls == []

    def test_replaced_trusted_checkpoint_is_rejected_before_runner(self, tmp_path):
        settings, _, workspace, _ = _prepare(tmp_path)
        (workspace / "preprocess.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
        checkpoint = workspace / "internal" / "gpt" / "ckpt" / "epoch=1-step=42.ckpt"

        def interrupted(command, *, cwd, env, log_path):
            _torch_zip(checkpoint)
            log_path.write_text("interrupted", encoding="utf-8")
            return 7

        with pytest.raises(CLIError, match="7"):
            run_trial_training(
                settings,
                workspace,
                "gpt",
                process_runner=interrupted,
                upstream_status_reader=lambda: [],
                checkpoint_inspector=_gpt_metadata,
            )
        checkpoint.write_bytes(checkpoint.read_bytes() + b"replacement")
        calls = []
        with pytest.raises(CLIError, match="哈希|替换|信任"):
            run_trial_training(
                settings,
                workspace,
                "gpt",
                process_runner=lambda *args, **kwargs: calls.append(kwargs["env"]),
                upstream_status_reader=lambda: [],
                checkpoint_inspector=_gpt_metadata,
            )
        assert calls == []

    @pytest.mark.parametrize("mutation", ["unknown", "path_escape"])
    def test_trusted_resume_rejects_unknown_checkpoint_or_recorded_path_escape(self, tmp_path, mutation):
        settings, _, workspace, _ = _prepare(tmp_path)
        (workspace / "preprocess.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
        checkpoint = workspace / "internal" / "gpt" / "ckpt" / "epoch=1-step=42.ckpt"

        def interrupted(command, *, cwd, env, log_path):
            _torch_zip(checkpoint)
            log_path.write_text("interrupted", encoding="utf-8")
            return 7

        with pytest.raises(CLIError, match="7"):
            run_trial_training(
                settings,
                workspace,
                "gpt",
                process_runner=interrupted,
                upstream_status_reader=lambda: [],
                checkpoint_inspector=_gpt_metadata,
            )
        trust_path = workspace / "gpt-resume-trust.json"
        trust = json.loads(trust_path.read_text(encoding="utf-8"))
        assert trust["checkpoints"][0]["sha256"] == _sha(checkpoint)
        if mutation == "unknown":
            _torch_zip(checkpoint.parent / "epoch=2-step=63.ckpt")
        else:
            trust["checkpoints"][0]["path"] = str(tmp_path / "outside" / checkpoint.name)
            trust_path.write_text(json.dumps(trust), encoding="utf-8")
        calls = []
        with pytest.raises(CLIError, match="未知|路径|信任|替换"):
            run_trial_training(
                settings,
                workspace,
                "gpt",
                process_runner=lambda *args, **kwargs: calls.append(kwargs["env"]),
                upstream_status_reader=lambda: [],
                checkpoint_inspector=_gpt_metadata,
            )
        assert calls == []

    def test_single_sovits_epoch_cannot_be_marked_completed(self, tmp_path):
        settings, _, workspace, _ = _prepare(tmp_path)
        (workspace / "preprocess.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")

        def runner(command, *, cwd, env, log_path):
            fixed = workspace / "features" / "logs_s2_v2ProPlus"
            _torch_zip(fixed / "G_87.pth")
            _torch_zip(fixed / "D_87.pth")
            _torch_zip(workspace / "checkpoints" / "sovits" / "speaker_e1_s87.pth")
            log_path.write_text("INFO:features:====> Epoch: 1\ntraining done\n", encoding="utf-8")
            return 0

        with pytest.raises(CLIError, match="8|数量|轮"):
            run_trial_training(
                settings,
                workspace,
                "sovits",
                process_runner=runner,
                upstream_status_reader=lambda: [],
                checkpoint_inspector=lambda path: {"format": "pytorch-zip", "top_level_keys": ["weight"]},
            )
        assert not (workspace / "train-sovits.json").exists()

    @pytest.mark.parametrize("target,corrupt_relative", [
        ("sovits", "features/logs_s2_v2ProPlus/G_87.pth"),
        ("gpt", "internal/gpt/ckpt/epoch=0-step=21.ckpt"),
    ])
    def test_corrupt_intermediate_checkpoint_prevents_completed_marker(self, tmp_path, target, corrupt_relative):
        settings, _, workspace, _ = _prepare(tmp_path)
        (workspace / "preprocess.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")

        def runner(command, *, cwd, env, log_path):
            if target == "sovits":
                _complete_sovits(workspace)
                log_path.write_text("INFO:features:====> Epoch: 8\ntraining done\n", encoding="utf-8")
            else:
                _complete_gpt(workspace)
                log_path.write_text("`Trainer.fit` stopped: `max_epochs=10` reached.\n", encoding="utf-8")
            (workspace / corrupt_relative).write_bytes(b"corrupt-intermediate")
            return 0

        with pytest.raises(CLIError, match="结构|ZIP|检查点"):
            run_trial_training(
                settings,
                workspace,
                target,
                process_runner=runner,
                upstream_status_reader=lambda: [],
                checkpoint_inspector=_gpt_metadata if target == "gpt" else lambda path: {
                    "format": "pytorch-zip", "top_level_keys": ["weight"]
                },
            )
        assert not (workspace / f"train-{target}.json").exists()

    def test_legacy_v1_plan_is_status_only_and_cannot_execute_or_overwrite(self, tmp_path):
        settings, manifest, workspace, _ = _prepare(tmp_path)
        plan_path = workspace / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["schema"] = "cli-anything-gpt-sovits/phase2b-plan-v1"
        plan.pop("artifacts", None)
        plan.pop("pretrained_models", None)
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        assert training_workspace_status(workspace)["approved_count"] == 3
        with pytest.raises(CLIError, match="版本|信任"):
            run_preprocessing(settings, workspace, dry_run=True)
        with pytest.raises(CLIError, match="版本|信任|覆盖"):
            prepare_training_workspace(settings, manifest, workspace, _sha(manifest), overwrite=True)


class TestTrainingExecution:
    def test_preprocess_dry_run_does_not_call_runner(self, tmp_path):
        settings, _, workspace, _ = _prepare(tmp_path)
        calls = []
        result = run_preprocessing(
            settings,
            workspace,
            dry_run=True,
            process_runner=lambda *args, **kwargs: calls.append(args),
            upstream_status_reader=lambda: [],
        )
        assert result["dry_run"] is True
        assert calls == []

    def test_preprocess_runs_four_required_upstream_actions_and_verifies_outputs(self, tmp_path):
        settings, _, workspace, _ = _prepare(tmp_path)
        calls = []

        def runner(command, *, cwd, env, log_path):
            calls.append(Path(command[2]).name)
            features = workspace / "features"
            if command[2].endswith("1-get-text.py"):
                (features / "2-name2text-0.txt").write_text("x\n" * 3, encoding="utf-8")
            elif command[2].endswith("2-get-hubert-wav32k.py"):
                (features / "4-cnhubert").mkdir(parents=True, exist_ok=True)
                (features / "5-wav32k").mkdir(parents=True, exist_ok=True)
                for index in range(3):
                    (features / "4-cnhubert" / f"speaker-{index:03d}.wav.pt").write_bytes(b"x")
                    (features / "5-wav32k" / f"speaker-{index:03d}.wav").write_bytes(b"x")
            elif command[2].endswith("2-get-sv.py"):
                (features / "7-sv_cn").mkdir(parents=True, exist_ok=True)
                for index in range(3):
                    (features / "7-sv_cn" / f"speaker-{index:03d}.wav.pt").write_bytes(b"x")
            else:
                (features / "6-name2semantic-0.tsv").write_text("x\n" * 3, encoding="utf-8")
            log_path.write_text("ok", encoding="utf-8")
            return 0

        result = run_preprocessing(settings, workspace, process_runner=runner, upstream_status_reader=lambda: [])
        assert calls == ["1-get-text.py", "2-get-hubert-wav32k.py", "2-get-sv.py", "3-get-semantic.py"]
        assert result["status"] == "completed"

    def test_preprocess_stops_on_subprocess_failure(self, tmp_path):
        settings, _, workspace, _ = _prepare(tmp_path)
        calls = []

        def runner(command, *, cwd, env, log_path):
            calls.append(Path(command[2]).name)
            log_path.write_text("failed", encoding="utf-8")
            return 7

        with pytest.raises(CLIError, match="7"):
            run_preprocessing(settings, workspace, process_runner=runner, upstream_status_reader=lambda: [])
        assert calls == ["1-get-text.py"]

    def test_resume_skips_three_verified_outputs_and_runs_only_semantic(self, tmp_path):
        settings, _, workspace, _ = _prepare(tmp_path)
        features = workspace / "features"
        (features / "2-name2text.txt").write_text("x\n" * 3, encoding="utf-8")
        for directory, suffix in (("4-cnhubert", ".wav.pt"), ("5-wav32k", ".wav"), ("7-sv_cn", ".wav.pt")):
            target = features / directory
            target.mkdir(exist_ok=True)
            for index in range(3):
                (target / f"speaker-{index:03d}{suffix}").write_bytes(b"x")
        s2_path = workspace / "configs" / "s2.json"
        parsed_before = json.loads(s2_path.read_text(encoding="utf-8"))
        calls = []
        semantic_config_paths = []

        def runner(command, *, cwd, env, log_path):
            calls.append(Path(command[2]).name)
            semantic_config_paths.append(env["s2config_path"])
            (features / "6-name2semantic-0.tsv").write_text("x\n" * 3, encoding="utf-8")
            log_path.write_text("ok", encoding="utf-8")
            return 0

        result = run_preprocessing(settings, workspace, process_runner=runner, upstream_status_reader=lambda: [])
        assert calls == ["3-get-semantic.py"]
        assert semantic_config_paths == [str(workspace / "configs" / "s2-preprocess.json")]
        assert "version" not in json.loads(Path(semantic_config_paths[0]).read_text(encoding="ascii"))["model"]
        assert [step["status"] for step in result["steps"]] == ["skipped_verified", "skipped_verified", "skipped_verified", "completed"]
        assert json.loads(s2_path.read_text(encoding="ascii")) == parsed_before

    def test_resume_rejects_incomplete_existing_output_instead_of_rerunning(self, tmp_path):
        settings, _, workspace, _ = _prepare(tmp_path)
        (workspace / "features" / "2-name2text.txt").write_text("x\n" * 28, encoding="utf-8")
        calls = []
        with pytest.raises(CLIError, match="数量"):
            run_preprocessing(
                settings,
                workspace,
                process_runner=lambda *args, **kwargs: calls.append(args),
                upstream_status_reader=lambda: [],
            )
        assert calls == []

    @pytest.mark.parametrize("status", [[" M GPT_SoVITS/configs/s2v2ProPlus.json"], ["?? unexpected.bin"]])
    def test_preprocess_rejects_unexpected_upstream_changes_before_runner(self, tmp_path, status):
        settings, _, workspace, _ = _prepare(tmp_path)
        calls = []
        with pytest.raises(CLIError, match="上游"):
            run_preprocessing(
                settings,
                workspace,
                process_runner=lambda *args, **kwargs: calls.append(args),
                upstream_status_reader=lambda: status,
            )
        assert calls == []

    @pytest.mark.parametrize("target", ["sovits", "gpt"])
    def test_training_dry_run_preserves_exact_parameters(self, tmp_path, target):
        settings, _, workspace, _ = _prepare(tmp_path)
        result = run_trial_training(settings, workspace, target, dry_run=True)
        assert result["dry_run"] is True
        assert result["parameters"]["batch_size"] == 1
        assert result["parameters"]["epochs"] == (8 if target == "sovits" else 10)

    def test_training_refuses_before_preprocessing(self, tmp_path):
        settings, _, workspace, _ = _prepare(tmp_path)
        with pytest.raises(CLIError, match="预处理"):
            run_trial_training(settings, workspace, "sovits")

    def test_training_rejects_unexpected_upstream_change_before_runner(self, tmp_path):
        settings, _, workspace, _ = _prepare(tmp_path)
        (workspace / "preprocess.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
        calls = []
        with pytest.raises(CLIError, match="上游"):
            run_trial_training(
                settings,
                workspace,
                "sovits",
                process_runner=lambda *args, **kwargs: calls.append(args),
                upstream_status_reader=lambda: ["?? unexpected.bin"],
            )
        assert calls == []

    def test_sovits_training_isolated_cwd_precreates_fixed_output_and_collects_all_checkpoints(self, tmp_path):
        settings, _, workspace, _ = _prepare(tmp_path)
        (workspace / "preprocess.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
        fixed_dir = workspace / "features" / "logs_s2_v2ProPlus"
        runtime_dir = workspace / "runtime-cwd" / "sovits"

        def runner(command, *, cwd, env, log_path):
            assert cwd == runtime_dir
            assert cwd != settings.checkout
            assert fixed_dir.is_dir()
            _complete_sovits(workspace)
            log_path.write_text("INFO:features:====> Epoch: 8\ntraining done\n", encoding="utf-8")
            return 0

        result = run_trial_training(
            settings,
            workspace,
            "sovits",
            process_runner=runner,
            upstream_status_reader=lambda: [],
            checkpoint_inspector=lambda path: {"format": "pytorch-zip", "top_level_keys": ["weight"]},
        )

        assert {item["kind"] for item in result["checkpoints"]} == {
            "full_discriminator",
            "full_generator",
            "lightweight",
        }
        assert len(result["checkpoints"]) == 24
        assert {"D_696.pth", "G_696.pth", "speaker_e8_s696.pth"}.issubset(
            {Path(item["path"]).name for item in result["checkpoints"]}
        )

    @pytest.mark.parametrize("target", ["sovits", "gpt"])
    def test_custom_speaker_checkpoint_names_complete_and_are_reported(self, tmp_path, target):
        settings = _settings(tmp_path)
        manifest = _manifest(tmp_path, language="en")
        workspace = tmp_path / "data" / "custom-speaker-workspace"
        prepare_training_workspace(
            settings,
            manifest,
            workspace,
            _sha(manifest),
            speaker="narrator",
            language="en",
            expected_approved_count=3,
        )
        (workspace / "preprocess.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")

        def runner(command, *, cwd, env, log_path):
            if target == "sovits":
                _complete_sovits(workspace, speaker="narrator")
                log_path.write_text("INFO:features:====> Epoch: 8\ntraining done\n", encoding="utf-8")
            else:
                _complete_gpt(workspace, speaker="narrator")
                log_path.write_text("`Trainer.fit` stopped: `max_epochs=10` reached.\n", encoding="utf-8")
            return 0

        result = run_trial_training(
            settings,
            workspace,
            target,
            process_runner=runner,
            upstream_status_reader=lambda: [],
            checkpoint_inspector=_gpt_metadata if target == "gpt" else lambda path: {
                "format": "pytorch-zip",
                "top_level_keys": ["weight"],
            },
        )

        lightweight = [Path(item["path"]).name for item in result["checkpoints"] if item["kind"] == "lightweight"]
        assert lightweight
        assert all(name.startswith("narrator-") if target == "gpt" else name.startswith("narrator_") for name in lightweight)
        status = training_workspace_status(workspace)
        assert status["stages"][target]["status"] == "completed"
        assert {item["sha256"] for item in status["checkpoints"]} == {item["sha256"] for item in result["checkpoints"]}

    @pytest.mark.parametrize("target", ["sovits", "gpt"])
    def test_custom_speaker_rejects_other_speaker_checkpoint_names(self, tmp_path, target):
        settings = _settings(tmp_path)
        manifest = _manifest(tmp_path, language="en")
        workspace = tmp_path / "data" / "custom-speaker-workspace"
        prepare_training_workspace(settings, manifest, workspace, _sha(manifest), speaker="narrator", language="en")
        (workspace / "preprocess.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")

        def runner(command, *, cwd, env, log_path):
            if target == "sovits":
                _complete_sovits(workspace, speaker="different-speaker")
                log_path.write_text("INFO:features:====> Epoch: 8\ntraining done\n", encoding="utf-8")
            else:
                _complete_gpt(workspace, speaker="different-speaker")
                log_path.write_text("`Trainer.fit` stopped: `max_epochs=10` reached.\n", encoding="utf-8")
            return 0

        with pytest.raises(CLIError, match="检查点|轮|配对"):
            run_trial_training(
                settings,
                workspace,
                target,
                process_runner=runner,
                upstream_status_reader=lambda: [],
                checkpoint_inspector=_gpt_metadata if target == "gpt" else lambda path: {
                    "format": "pytorch-zip",
                    "top_level_keys": ["weight"],
                },
            )
        assert not (workspace / f"train-{target}.json").exists()

    def test_training_recovery_preserves_existing_failure_log(self, tmp_path):
        settings, _, workspace, _ = _prepare(tmp_path)
        (workspace / "preprocess.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
        original_log = workspace / "logs" / "train-sovits.log"
        original_log.write_text("epoch 1 save failed", encoding="utf-8")

        def runner(command, *, cwd, env, log_path):
            assert log_path == workspace / "logs" / "train-sovits-resume1.log"
            _complete_sovits(workspace)
            log_path.write_text("INFO:features:====> Epoch: 8\ntraining done\n", encoding="utf-8")
            return 0

        result = run_trial_training(
            settings,
            workspace,
            "sovits",
            process_runner=runner,
            upstream_status_reader=lambda: [],
            checkpoint_inspector=lambda path: {"format": "pytorch-zip", "top_level_keys": ["weight"]},
        )

        assert original_log.read_text(encoding="utf-8") == "epoch 1 save failed"
        assert result["log"].endswith("train-sovits-resume1.log")

    def test_gpt_resume_uses_narrow_safe_globals_launcher_and_clears_parent_unsafe_env(self, tmp_path, monkeypatch):
        settings, _, workspace, _ = _prepare(tmp_path)
        monkeypatch.setenv("TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD", "1")
        monkeypatch.setenv("TORCH_FORCE_WEIGHTS_ONLY_LOAD", "1")
        (workspace / "preprocess.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
        original_log = workspace / "logs" / "train-gpt.log"
        original_log.write_text("external interruption after epoch 2", encoding="utf-8")
        full_dir = workspace / "internal" / "gpt" / "ckpt"
        latest = full_dir / "epoch=1-step=42.ckpt"

        def interrupted(command, *, cwd, env, log_path):
            assert "TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD" not in env
            assert "TORCH_FORCE_WEIGHTS_ONLY_LOAD" not in env
            _torch_zip(full_dir / "epoch=0-step=21.ckpt")
            _torch_zip(latest)
            log_path.write_text("external interruption", encoding="utf-8")
            return 7

        with pytest.raises(CLIError, match="7"):
            run_trial_training(
                settings,
                workspace,
                "gpt",
                process_runner=interrupted,
                upstream_status_reader=lambda: [],
                checkpoint_inspector=_gpt_metadata,
            )
        trusted_resume_sha = _sha(latest)

        def runner(command, *, cwd, env, log_path):
            assert "TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD" not in env
            assert "TORCH_FORCE_WEIGHTS_ONLY_LOAD" not in env
            assert env["PYTHONUTF8"] == "1"
            assert env["PYTHONIOENCODING"] == "utf-8"
            launcher = Path(command[2])
            assert launcher.name == "gpt_resume_launcher.py"
            launcher_text = launcher.read_text(encoding="ascii")
            assert "safe_globals" in launcher_text
            assert "WindowsPath" in launcher_text
            assert "weights_only=False" not in launcher_text
            assert "if candidate == resume_path" in launcher_text
            assert "return original_load(source, *args, **kwargs)" in launcher_text
            assert str(latest) in command and trusted_resume_sha in command
            assert log_path == workspace / "logs" / "train-gpt-resume2.log"
            _complete_gpt(workspace)
            log_path.write_text(
                f"ckpt_path: {latest}\nRestored all states from the checkpoint at {latest}\n"
                "`Trainer.fit` stopped: `max_epochs=10` reached.\n",
                encoding="utf-8",
            )
            return 0

        result = run_trial_training(
            settings,
            workspace,
            "gpt",
            process_runner=runner,
            upstream_status_reader=lambda: [],
            checkpoint_inspector=_gpt_metadata,
        )

        assert original_log.read_text(encoding="utf-8") == "external interruption after epoch 2"
        assert result["resume_checkpoint"]["path"] == str(latest)
        assert result["resume_checkpoint"]["sha256"] == trusted_resume_sha
        assert result["parameters"]["epochs"] == 10

    def test_gpt_resume_requires_restored_all_states_log_proof(self, tmp_path):
        settings, _, workspace, _ = _prepare(tmp_path)
        (workspace / "preprocess.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
        latest = workspace / "internal" / "gpt" / "ckpt" / "epoch=1-step=42.ckpt"

        def interrupted(command, *, cwd, env, log_path):
            _torch_zip(workspace / "internal" / "gpt" / "ckpt" / "epoch=0-step=21.ckpt")
            _torch_zip(latest)
            log_path.write_text("external interruption", encoding="utf-8")
            return 7

        with pytest.raises(CLIError, match="7"):
            run_trial_training(
                settings,
                workspace,
                "gpt",
                process_runner=interrupted,
                upstream_status_reader=lambda: [],
                checkpoint_inspector=_gpt_metadata,
            )

        def missing_restored(command, *, cwd, env, log_path):
            _complete_gpt(workspace)
            log_path.write_text(
                f"ckpt_path: {latest}\n`Trainer.fit` stopped: `max_epochs=10` reached.\n",
                encoding="utf-8",
            )
            return 0

        with pytest.raises(CLIError, match="Restored|恢复"):
            run_trial_training(
                settings,
                workspace,
                "gpt",
                process_runner=missing_restored,
                upstream_status_reader=lambda: [],
                checkpoint_inspector=_gpt_metadata,
            )
        assert not (workspace / "train-gpt.json").exists()

    def test_status_lists_checkpoint_size_and_hash(self, tmp_path):
        _, _, workspace, _ = _prepare(tmp_path)
        checkpoint = workspace / "checkpoints" / "sovits" / "speaker-e1.pth"
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_bytes(b"checkpoint")
        status = training_workspace_status(workspace)
        assert status["checkpoints"][0]["sha256"] == _sha(checkpoint)
        assert status["checkpoints"][0]["size_bytes"] == len(b"checkpoint")


class TestTrainingCLI:
    @staticmethod
    def _base(settings: Settings) -> list[str]:
        return [
            "--checkout",
            str(settings.checkout),
            "--runtime",
            str(settings.runtime),
            "--tts-config",
            str(settings.tts_config),
            "--state-dir",
            str(settings.state_dir),
        ]

    def test_training_help_lists_phase2b_commands(self, tmp_path):
        settings = _settings(tmp_path)
        result = CliRunner().invoke(cli, [*self._base(settings), "training", "--help"])
        assert result.exit_code == 0
        assert all(name in result.output for name in ("plan", "preprocess", "run", "status"))

    def test_plan_dry_run_json_is_stable_and_has_no_side_effect(self, tmp_path):
        settings = _settings(tmp_path)
        manifest = _manifest(tmp_path)
        workspace = tmp_path / "data" / "work"
        result = CliRunner().invoke(
            cli,
            [
                *self._base(settings),
                "training",
                "plan",
                "--manifest",
                str(manifest),
                "--workspace",
                str(workspace),
                "--expected-manifest-sha256",
                _sha(manifest),
                "--dry-run",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.output)
        assert envelope["ok"] is True
        assert envelope["command"] == "training.plan"
        assert envelope["data"]["approved_count"] == 3
        assert not workspace.exists()

    def test_status_json_reports_real_checkpoint(self, tmp_path):
        settings, _, workspace, _ = _prepare(tmp_path)
        checkpoint = workspace / "checkpoints" / "gpt" / "speaker-e1.ckpt"
        checkpoint.write_bytes(b"real-structure-fixture")
        result = CliRunner().invoke(
            cli,
            [*self._base(settings), "training", "status", "--workspace", str(workspace), "--json"],
        )
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.output)
        assert envelope["ok"] is True
        assert envelope["data"]["checkpoints"][0]["sha256"] == _sha(checkpoint)

    def test_preprocess_dry_run_json_does_not_execute_backend(self, tmp_path):
        settings, _, workspace, _ = _prepare(tmp_path)
        result = CliRunner().invoke(
            cli,
            [*self._base(settings), "training", "preprocess", "--workspace", str(workspace), "--dry-run", "--json"],
        )
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.output)
        assert envelope["data"]["dry_run"] is True
        assert len(envelope["data"]["steps"]) == 4
        assert not (workspace / "preprocess.json").exists()
