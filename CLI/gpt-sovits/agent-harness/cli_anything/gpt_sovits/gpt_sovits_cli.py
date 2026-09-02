from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

import click

from cli_anything.gpt_sovits import __version__
from cli_anything.gpt_sovits.core.audio import inspect_wav
from cli_anything.gpt_sovits.core.config import Settings
from cli_anything.gpt_sovits.core.doctor import run_doctor
from cli_anything.gpt_sovits.core.dataset import build_manifest, extract_clip, find_duplicate_hashes, inspect_training_wav
from cli_anything.gpt_sovits.core.errors import CLIError
from cli_anything.gpt_sovits.core.models import list_models, use_weight
from cli_anything.gpt_sovits.core.output import emit, fail
from cli_anything.gpt_sovits.core.phase2b import (
    prepare_training_workspace,
    run_preprocessing,
    run_trial_training,
    training_workspace_status,
)
from cli_anything.gpt_sovits.core.paths import require_local_path
from cli_anything.gpt_sovits.core.service import logs as service_logs
from cli_anything.gpt_sovits.core.service import start as service_start
from cli_anything.gpt_sovits.core.service import status as service_status
from cli_anything.gpt_sovits.core.service import stop as service_stop
from cli_anything.gpt_sovits.core.training import training_doctor
from cli_anything.gpt_sovits.core.uvr import APPROVED_UVR_URL, download_uvr_archive
from cli_anything.gpt_sovits.core.workflow import (
    build_listening_index,
    build_proofreading_index,
    create_uvr_comparison,
    prepare_records,
    transcribe_candidates,
)
from cli_anything.gpt_sovits.utils.gpt_sovits_backend import synthesize as backend_synthesize
from cli_anything.gpt_sovits.utils.repl_skin import ReplSkin


CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 110}
_JSON_REQUESTED = False


def _use_json(ctx: click.Context, local: bool) -> bool:
    return bool(local or ctx.find_root().obj["json"])


def _dry_run(ctx: click.Context, local: bool) -> bool:
    return bool(local or ctx.find_root().obj["dry_run"])


def _settings(ctx: click.Context) -> Settings:
    return ctx.find_root().obj["settings"]


def output_options(function):
    function = click.option("--json", "command_json", is_flag=True, help="输出稳定 JSON 封装")(function)
    return function


def mutation_options(function):
    function = click.option("--dry-run", "command_dry_run", is_flag=True, help="只展示计划，不产生副作用")(function)
    function = output_options(function)
    return function


def _run(command: str, ctx: click.Context, command_json: bool, callback):
    use_json = _use_json(ctx, command_json)
    try:
        data = callback()
        emit(command, data, use_json)
        return data
    except Exception as exc:
        fail(command, exc, use_json)


@click.group(invoke_without_command=True, context_settings=CONTEXT_SETTINGS)
@click.option("--checkout", type=click.Path(path_type=str), help="GPT-SoVITS 源码目录")
@click.option("--runtime", type=click.Path(path_type=str), help="GPT-SoVITS Python 可执行文件")
@click.option("--api-url", help="本机 API 地址，默认 http://127.0.0.1:9880")
@click.option("--tts-config", type=click.Path(path_type=str), help="tts_infer.yaml 路径")
@click.option("--state-dir", type=click.Path(path_type=str), help="CLI 服务状态和日志目录")
@click.option("--json", "use_json", is_flag=True, help="输出稳定 JSON 封装")
@click.option("--dry-run", is_flag=True, help="所有变更命令只展示计划")
@click.version_option(__version__)
@click.pass_context
def cli(ctx, checkout, runtime, api_url, tts_config, state_dir, use_json, dry_run):
    """通过本机真实 GPT-SoVITS API 进行诊断、服务管理和语音合成。"""
    try:
        settings = Settings.discover(checkout, runtime, api_url, tts_config, state_dir)
    except Exception as exc:
        fail("config", exc, bool(use_json or _JSON_REQUESTED))
        return
    ctx.ensure_object(dict)
    ctx.obj.update({"settings": settings, "json": use_json, "dry_run": dry_run})
    if ctx.invoked_subcommand is None:
        ctx.invoke(repl)


@cli.command()
@output_options
@click.pass_context
def doctor(ctx, command_json):
    """只读检查源码、Python、模型、GPU、FFmpeg 和 API。"""
    return _run("doctor", ctx, command_json, lambda: run_doctor(_settings(ctx)))


@cli.group()
def serve():
    """安全管理由本 CLI 启动的本机 API 服务。"""


@serve.command("start")
@click.option("--timeout", type=click.FloatRange(min=1), default=300.0, show_default=True, help="等待模型服务就绪的秒数")
@mutation_options
@click.pass_context
def serve_start_cmd(ctx, timeout, command_json, command_dry_run):
    """启动真实 api_v2.py 并等待就绪。"""
    return _run("serve.start", ctx, command_json, lambda: service_start(_settings(ctx), timeout, _dry_run(ctx, command_dry_run)))


@serve.command("status")
@output_options
@click.pass_context
def serve_status_cmd(ctx, command_json):
    """检查启动记录、PID 身份和 API 可达性。"""
    return _run("serve.status", ctx, command_json, lambda: service_status(_settings(ctx), record_event=True))


@serve.command("stop")
@click.option("--timeout", type=click.FloatRange(min=1), default=20.0, show_default=True)
@mutation_options
@click.pass_context
def serve_stop_cmd(ctx, timeout, command_json, command_dry_run):
    """仅在 PID、程序和命令行全部匹配时停止服务。"""
    return _run("serve.stop", ctx, command_json, lambda: service_stop(_settings(ctx), timeout, _dry_run(ctx, command_dry_run)))


@serve.command("logs")
@click.option("--lines", type=click.IntRange(min=1, max=5000), default=80, show_default=True)
@output_options
@click.pass_context
def serve_logs_cmd(ctx, lines, command_json):
    """读取服务日志末尾内容。"""
    return _run("serve.logs", ctx, command_json, lambda: service_logs(_settings(ctx), lines))


@cli.group()
def model():
    """枚举或切换本机 GPT 与 SoVITS 权重。"""


@model.command("list")
@output_options
@click.pass_context
def model_list_cmd(ctx, command_json):
    """列出本机可见的 .ckpt 与 .pth 权重。"""
    return _run("model.list", ctx, command_json, lambda: list_models(_settings(ctx)))


@model.command("use-gpt")
@click.argument("path", type=click.Path(path_type=str))
@click.option("--timeout", type=click.FloatRange(min=1), default=120.0, show_default=True)
@mutation_options
@click.pass_context
def model_use_gpt_cmd(ctx, path, timeout, command_json, command_dry_run):
    """让运行中的真实后端切换 GPT 权重。"""
    return _run("model.use-gpt", ctx, command_json, lambda: use_weight(_settings(ctx), path, "gpt", _dry_run(ctx, command_dry_run), timeout))


@model.command("use-sovits")
@click.argument("path", type=click.Path(path_type=str))
@click.option("--timeout", type=click.FloatRange(min=1), default=120.0, show_default=True)
@mutation_options
@click.pass_context
def model_use_sovits_cmd(ctx, path, timeout, command_json, command_dry_run):
    """让运行中的真实后端切换 SoVITS 权重。"""
    return _run("model.use-sovits", ctx, command_json, lambda: use_weight(_settings(ctx), path, "sovits", _dry_run(ctx, command_dry_run), timeout))


@cli.group()
def reference():
    """检查参考音频。"""


@reference.command("inspect")
@click.argument("audio", type=click.Path(path_type=str))
@output_options
@click.pass_context
def reference_inspect_cmd(ctx, audio, command_json):
    """验证 WAV 结构并报告时长、RMS 与哈希。"""
    return _run("reference.inspect", ctx, command_json, lambda: inspect_wav(audio))


@cli.group()
def training():
    """只读检查数据准备和后续训练环境。"""


@training.command("doctor")
@click.option("--asr-cache", type=click.Path(path_type=str), help="本机 faster-whisper-base 离线快照")
@click.option("--uvr-dir", type=click.Path(path_type=str), help="本机 UVR5 权重目录")
@click.option("--data-dir", type=click.Path(path_type=str), help="数据集目标目录，用于磁盘检查")
@output_options
@click.pass_context
def training_doctor_cmd(ctx, asr_cache, uvr_dir, data_dir, command_json):
    """只读报告训练脚本、GPU、磁盘、FFmpeg、ASR 和 UVR5。"""
    return _run(
        "training.doctor",
        ctx,
        command_json,
        lambda: training_doctor(_settings(ctx), asr_cache=asr_cache, uvr_dir=uvr_dir, data_dir=data_dir),
    )


@training.command("plan")
@click.option("--manifest", required=True, type=click.Path(path_type=str), help="阶段 2A 正式 manifest.jsonl")
@click.option("--workspace", required=True, type=click.Path(path_type=str), help="数据目录下的新阶段 2B 工作区")
@click.option("--expected-manifest-sha256", required=True, help="已批准 manifest 的 SHA-256")
@click.option("--speaker", default="speaker", show_default=True, help="安全的说话人标识，用于标签和检查点命名")
@click.option("--language", help="训练文本语言代码；省略时从唯一 text_<语言> 字段推断")
@click.option("--expected-approved-count", type=click.IntRange(min=1), help="可选的 approved 数量门；省略时采用实际数量")
@click.option("--overwrite", is_flag=True, help="仅覆盖已有阶段 2B 标记工作区")
@mutation_options
@click.pass_context
def training_plan_cmd(
    ctx,
    manifest,
    workspace,
    expected_manifest_sha256,
    speaker,
    language,
    expected_approved_count,
    overwrite,
    command_json,
    command_dry_run,
):
    """生成 approved-only 标签、快照和隔离配置。"""
    return _run(
        "training.plan",
        ctx,
        command_json,
        lambda: prepare_training_workspace(
            _settings(ctx),
            manifest,
            workspace,
            expected_manifest_sha256,
            overwrite=overwrite,
            dry_run=_dry_run(ctx, command_dry_run),
            speaker=speaker,
            language=language,
            expected_approved_count=expected_approved_count,
        ),
    )


@training.command("preprocess")
@click.option("--workspace", required=True, type=click.Path(path_type=str), help="已生成计划的阶段 2B 工作区")
@mutation_options
@click.pass_context
def training_preprocess_cmd(ctx, workspace, command_json, command_dry_run):
    """依次运行文本、HuBERT、说话人向量和语义预处理。"""
    return _run(
        "training.preprocess",
        ctx,
        command_json,
        lambda: run_preprocessing(_settings(ctx), workspace, dry_run=_dry_run(ctx, command_dry_run)),
    )


@training.command("run")
@click.option("--workspace", required=True, type=click.Path(path_type=str), help="已完成预处理的阶段 2B 工作区")
@click.option("--target", required=True, type=click.Choice(["sovits", "gpt"]), help="只运行一个明确训练目标")
@mutation_options
@click.pass_context
def training_run_cmd(ctx, workspace, target, command_json, command_dry_run):
    """按批准的 batch/epochs 运行一次保守试训练。"""
    return _run(
        "training.run",
        ctx,
        command_json,
        lambda: run_trial_training(_settings(ctx), workspace, target, dry_run=_dry_run(ctx, command_dry_run)),
    )


@training.command("status")
@click.option("--workspace", required=True, type=click.Path(path_type=str), help="阶段 2B 工作区")
@output_options
@click.pass_context
def training_status_cmd(ctx, workspace, command_json):
    """只读查看阶段、日志和检查点哈希。"""
    return _run("training.status", ctx, command_json, lambda: training_workspace_status(workspace))


@training.command("download-uvr")
@click.option("--url", default=APPROVED_UVR_URL, show_default=True, help="阶段 2A 唯一获准的 UVR5 包")
@click.option("--output", required=True, type=click.Path(path_type=str), help="本机 ZIP 输出")
@click.option("--expected-size", type=click.IntRange(min=1), help="可选预期字节数")
@click.option("--expected-sha256", help="可选预期 SHA-256")
@click.option("--overwrite", is_flag=True, help="显式覆盖已有下载")
@mutation_options
@click.pass_context
def training_download_uvr_cmd(ctx, url, output, expected_size, expected_sha256, overwrite, command_json, command_dry_run):
    """按获批 URL 流式下载并校验 UVR5 ZIP；不会自动解压。"""
    return _run(
        "training.download-uvr",
        ctx,
        command_json,
        lambda: download_uvr_archive(
            url,
            output,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
            overwrite=overwrite,
            dry_run=_dry_run(ctx, command_dry_run),
        ),
    )


@cli.group()
def dataset():
    """提取、检查并记录可追溯训练候选。"""


@dataset.command("extract")
@click.option("--source", required=True, type=click.Path(path_type=str), help="本机源媒体")
@click.option("--start", required=True, help="开始时间码：秒数或 HH:MM:SS.mmm")
@click.option("--end", required=True, help="结束时间码：秒数或 HH:MM:SS.mmm")
@click.option("--output", required=True, type=click.Path(path_type=str), help="输出 32kHz 单声道 PCM WAV")
@click.option("--overwrite", is_flag=True, help="显式覆盖已有输出")
@mutation_options
@click.pass_context
def dataset_extract_cmd(ctx, source, start, end, output, overwrite, command_json, command_dry_run):
    """用真实 FFmpeg 按显式时间码提取 2–10 秒候选。"""
    return _run(
        "dataset.extract",
        ctx,
        command_json,
        lambda: extract_clip(source, start, end, output, overwrite=overwrite, dry_run=_dry_run(ctx, command_dry_run)),
    )


@dataset.command("inspect")
@click.argument("audio", nargs=-1, required=True, type=click.Path(path_type=str))
@click.option(
    "--text-ja",
    multiple=True,
    help="与 AUDIO 顺序一一对应的日语标注；提供后会检查空标注",
)
@output_options
@click.pass_context
def dataset_inspect_cmd(ctx, audio, text_ja, command_json):
    """检查训练 WAV 格式、标注、静音、削波和重复哈希。"""

    def action():
        if text_ja and len(text_ja) != len(audio):
            raise CLIError(
                "annotation_count_mismatch",
                "--text-ja 数量必须与 AUDIO 数量一致",
                {"audio_count": len(audio), "annotation_count": len(text_ja)},
            )
        reports = [inspect_training_wav(path) for path in audio]
        duplicates = find_duplicate_hashes(reports)
        empty_indices = [index for index, text in enumerate(text_ja) if not text.strip()]
        annotations = {
            "checked": bool(text_ja),
            "empty_indices": empty_indices,
            "compliant": bool(text_ja) and not empty_indices,
        }
        return {
            "reports": reports,
            "duplicates": duplicates,
            "annotations": annotations,
            "compliant": (
                all(report["compliant"] for report in reports)
                and not duplicates
                and (not text_ja or annotations["compliant"])
            ),
        }

    return _run("dataset.inspect", ctx, command_json, action)


def _load_records(path: str) -> list[dict]:
    records_path = require_local_path(path, purpose="records")
    if not records_path.is_file():
        raise CLIError("records_not_found", "找不到 UTF-8 JSON 记录文件", {"path": str(records_path)})
    try:
        records = json.loads(records_path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise CLIError("invalid_records_encoding", "记录文件必须是 UTF-8", {"path": str(records_path)}) from exc
    except json.JSONDecodeError as exc:
        raise CLIError("invalid_records_json", "记录文件不是有效 JSON", {"path": str(records_path), "line": exc.lineno}) from exc
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise CLIError("invalid_records_json", "记录文件必须是 JSON 对象数组", {"path": str(records_path)})
    return records


@dataset.command("manifest")
@click.option("--records", required=True, type=click.Path(path_type=str), help="UTF-8 JSON 对象数组")
@click.option("--output", required=True, type=click.Path(path_type=str), help="输出 JSONL 清单")
@click.option("--overwrite", is_flag=True, help="显式覆盖已有清单")
@mutation_options
@click.pass_context
def dataset_manifest_cmd(ctx, records, output, overwrite, command_json, command_dry_run):
    """原子生成稳定 JSONL 清单；不会自动标记 approved。"""
    return _run(
        "dataset.manifest",
        ctx,
        command_json,
        lambda: build_manifest(_load_records(records), output, overwrite=overwrite, dry_run=_dry_run(ctx, command_dry_run)),
    )


@dataset.command("transcribe")
@click.argument("audio", nargs=-1, required=True, type=click.Path(path_type=str))
@click.option("--output", required=True, type=click.Path(path_type=str), help="离线 ASR JSONL 草稿")
@click.option("--runtime", required=True, type=click.Path(path_type=str), help="已安装 faster-whisper 的本机 Python")
@click.option("--asr-cache", required=True, type=click.Path(path_type=str), help="本机离线模型快照")
@click.option("--overwrite", is_flag=True, help="显式覆盖已有草稿")
@mutation_options
@click.pass_context
def dataset_transcribe_cmd(ctx, audio, output, runtime, asr_cache, overwrite, command_json, command_dry_run):
    """离线生成 pending ASR 草稿并钳制分段到真实音频边界。"""
    return _run(
        "dataset.transcribe",
        ctx,
        command_json,
        lambda: transcribe_candidates(
            list(audio), output, runtime=runtime, model_path=asr_cache, overwrite=overwrite, dry_run=_dry_run(ctx, command_dry_run)
        ),
    )


@dataset.command("prepare")
@click.option("--plan", "plan_path", required=True, type=click.Path(path_type=str), help="候选计划 JSON")
@click.option("--drafts", "drafts_path", required=True, type=click.Path(path_type=str), help="ASR 草稿 JSONL")
@click.option("--output", required=True, type=click.Path(path_type=str), help="manifest records JSON")
@click.option("--inspection-output", required=True, type=click.Path(path_type=str), help="音频检查 JSON")
@click.option("--overwrite", is_flag=True, help="显式覆盖已有产物")
@mutation_options
@click.pass_context
def dataset_prepare_cmd(ctx, plan_path, drafts_path, output, inspection_output, overwrite, command_json, command_dry_run):
    """把候选计划和离线草稿合并为仍为 pending 的清单记录。"""
    return _run(
        "dataset.prepare",
        ctx,
        command_json,
        lambda: prepare_records(
            plan_path, drafts_path, output, inspection_output, overwrite=overwrite, dry_run=_dry_run(ctx, command_dry_run)
        ),
    )


@dataset.command("index")
@click.option("--manifest", required=True, type=click.Path(path_type=str), help="UTF-8 JSONL 清单")
@click.option("--output", required=True, type=click.Path(path_type=str), help="本机试听 HTML")
@click.option("--comparison-dir", type=click.Path(path_type=str), help="可选 UVR5 对照目录")
@click.option("--overwrite", is_flag=True, help="显式覆盖已有索引")
@mutation_options
@click.pass_context
def dataset_index_cmd(ctx, manifest, output, comparison_dir, overwrite, command_json, command_dry_run):
    """从清单生成只读本机试听索引。"""
    return _run(
        "dataset.index",
        ctx,
        command_json,
        lambda: build_listening_index(
            manifest, output, comparison_dir=comparison_dir, overwrite=overwrite, dry_run=_dry_run(ctx, command_dry_run)
        ),
    )


@dataset.command("proofread-index")
@click.option("--manifest", required=True, type=click.Path(path_type=str), help="UTF-8 JSONL 正式清单")
@click.option("--proposals", required=True, type=click.Path(path_type=str), help="独立日语校对建议 JSON")
@click.option("--output", required=True, type=click.Path(path_type=str), help="本机只读校对 HTML")
@click.option("--overwrite", is_flag=True, help="显式覆盖已有校对页")
@mutation_options
@click.pass_context
def dataset_proofread_index_cmd(ctx, manifest, proposals, output, overwrite, command_json, command_dry_run):
    """并列展示原音、中文字幕、ASR 草稿和建议日语，不回写清单。"""
    return _run(
        "dataset.proofread_index",
        ctx,
        command_json,
        lambda: build_proofreading_index(
            manifest,
            proposals,
            output,
            overwrite=overwrite,
            dry_run=_dry_run(ctx, command_dry_run),
        ),
    )


@dataset.command("uvr-compare")
@click.option("--audio", required=True, type=click.Path(path_type=str), help="候选 WAV")
@click.option("--output", required=True, type=click.Path(path_type=str), help="UVR5 对照 WAV")
@click.option("--report-output", required=True, type=click.Path(path_type=str), help="对照证据 JSON")
@click.option("--runtime", required=True, type=click.Path(path_type=str), help="GPT-SoVITS 隔离 Python")
@click.option("--weight", required=True, type=click.Path(path_type=str), help="获准的本机 HP5 权重")
@click.option("--overwrite", is_flag=True, help="显式覆盖已有对照与报告")
@mutation_options
@click.pass_context
def dataset_uvr_compare_cmd(ctx, audio, output, report_output, runtime, weight, overwrite, command_json, command_dry_run):
    """生成一条 pending UVR5 HP5 试听对照。"""
    settings = _settings(ctx)
    return _run(
        "dataset.uvr-compare",
        ctx,
        command_json,
        lambda: create_uvr_comparison(
            audio,
            output,
            report_output,
            runtime=runtime,
            checkout=settings.checkout,
            weight=weight,
            overwrite=overwrite,
            dry_run=_dry_run(ctx, command_dry_run),
        ),
    )


def _load_text(text: str | None, text_file: str | None) -> str:
    if bool(text) == bool(text_file):
        raise CLIError("invalid_text_source", "必须且只能提供 --text 或 --text-file 其中一个")
    if text_file:
        path = Path(text_file).resolve()
        if not path.is_file():
            raise CLIError("text_file_not_found", "找不到 UTF-8 文本文件", {"path": str(path)})
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise CLIError("invalid_text_encoding", "文本文件必须是 UTF-8 编码", {"path": str(path)}) from exc
    result = (text or "").strip()
    if not result:
        raise CLIError("empty_text", "合成文本不能为空")
    return result


def _split_repl_line(line: str) -> list[str]:
    if os.name != "nt":
        return shlex.split(line)
    import ctypes

    argc = ctypes.c_int()
    command_line_to_argv = ctypes.windll.shell32.CommandLineToArgvW
    command_line_to_argv.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    command_line_to_argv.restype = ctypes.POINTER(ctypes.c_wchar_p)
    argv = command_line_to_argv(line, ctypes.byref(argc))
    if not argv:
        raise CLIError("invalid_repl_command", "无法解析 REPL 命令行")
    try:
        return [argv[index] for index in range(argc.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(argv)


def _emit_click_error(exc: click.ClickException) -> None:
    from cli_anything.gpt_sovits.core.output import envelope
    import json

    click.echo(
        json.dumps(
            envelope(
                "usage",
                error={"code": "usage_error", "message": exc.format_message(), "details": {"exit_code": exc.exit_code}},
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


@cli.command("synthesize")
@click.option("--text", help="要合成的文本")
@click.option("--text-file", type=click.Path(path_type=str), help="UTF-8 文本文件")
@click.option("--text-lang", required=True, help="目标文本语言，例如 zh")
@click.option("--ref-audio", required=True, type=click.Path(path_type=str), help="本机参考 WAV")
@click.option("--prompt-lang", required=True, help="参考音频文本语言，例如 zh")
@click.option("--prompt-text", default="", help="参考音频的准确文本")
@click.option("--output", required=True, type=click.Path(path_type=str), help="输出 WAV 路径")
@click.option("--overwrite", is_flag=True, help="显式覆盖已有输出")
@click.option("--text-split-method", default="cut5", show_default=True)
@click.option("--top-k", type=click.IntRange(min=1), default=15, show_default=True)
@click.option("--top-p", type=click.FloatRange(min=0.0, max=1.0, min_open=True), default=1.0, show_default=True)
@click.option("--temperature", type=click.FloatRange(min=0.0, min_open=True), default=1.0, show_default=True)
@click.option("--batch-size", type=click.IntRange(min=1), default=1, show_default=True)
@click.option("--speed-factor", type=click.FloatRange(min=0.1, max=4.0), default=1.0, show_default=True)
@click.option("--fragment-interval", type=click.FloatRange(min=0.0), default=0.3, show_default=True)
@click.option("--seed", type=int, default=-1, show_default=True)
@click.option("--repetition-penalty", type=click.FloatRange(min=0.0, min_open=True), default=1.35, show_default=True)
@click.option("--split-bucket/--no-split-bucket", default=True, show_default=True)
@click.option("--parallel-infer/--no-parallel-infer", default=True, show_default=True)
@click.option("--timeout", type=click.FloatRange(min=1), default=600.0, show_default=True)
@mutation_options
@click.pass_context
def synthesize_cmd(
    ctx,
    text,
    text_file,
    text_lang,
    ref_audio,
    prompt_lang,
    prompt_text,
    output,
    overwrite,
    text_split_method,
    top_k,
    top_p,
    temperature,
    batch_size,
    speed_factor,
    fragment_interval,
    seed,
    repetition_penalty,
    split_bucket,
    parallel_infer,
    timeout,
    command_json,
    command_dry_run,
):
    """调用真实 GPT-SoVITS API 合成非流式 WAV。"""

    def action():
        final_text = _load_text(text, text_file)
        reference_path = Path(ref_audio).resolve()
        inspect_wav(reference_path)
        output_path = Path(output).resolve()
        if output_path.suffix.lower() != ".wav":
            raise CLIError("unsupported_output", "阶段一真实验收只支持 .wav 输出")
        if output_path.exists() and not overwrite:
            raise CLIError("output_exists", "输出文件已存在；如需覆盖请加 --overwrite", {"path": str(output_path)})
        payload = {
            "text": final_text,
            "text_lang": text_lang.lower(),
            "ref_audio_path": str(reference_path),
            "prompt_lang": prompt_lang.lower(),
            "prompt_text": prompt_text,
            "top_k": top_k,
            "top_p": top_p,
            "temperature": temperature,
            "text_split_method": text_split_method,
            "batch_size": batch_size,
            "batch_threshold": 0.75,
            "split_bucket": split_bucket,
            "speed_factor": speed_factor,
            "fragment_interval": fragment_interval,
            "seed": seed,
            "media_type": "wav",
            "streaming_mode": False,
            "parallel_infer": parallel_infer,
            "repetition_penalty": repetition_penalty,
        }
        if _dry_run(ctx, command_dry_run):
            return {"dry_run": True, "action": "synthesize", "api_url": _settings(ctx).api_url, "output": str(output_path), "parameters": payload}
        return backend_synthesize(_settings(ctx).api_url, payload, output_path, overwrite, timeout)

    return _run("synthesize", ctx, command_json, action)


@cli.command(hidden=True)
@click.pass_context
def repl(ctx):
    """进入统一样式交互模式。"""
    global _JSON_REQUESTED
    skin = ReplSkin("gpt-sovits", version=__version__)
    skin.print_banner()
    skin.help(
        {
            "doctor --json": "检查运行环境",
            "serve start|status|stop|logs": "管理本机服务",
            "model list|use-gpt|use-sovits": "查看或切换模型",
            "reference inspect <wav>": "检查参考音频",
            "training doctor --json": "检查数据准备和训练环境",
            "dataset extract|inspect|manifest": "准备可试听训练候选",
            "synthesize ...": "合成 WAV",
            "exit / quit": "退出",
        }
    )
    session = skin.create_prompt_session() if sys.stdin.isatty() and sys.stdout.isatty() else None
    settings = _settings(ctx)
    prefix = [
        "--checkout", str(settings.checkout),
        "--runtime", str(settings.runtime),
        "--api-url", settings.api_url,
        "--tts-config", str(settings.tts_config),
        "--state-dir", str(settings.state_dir),
    ]
    while True:
        try:
            line = skin.get_input(session, context="local")
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line.lower() in {"exit", "quit"}:
            break
        if line.lower() == "help":
            line = "--help"
        previous_json_requested = _JSON_REQUESTED
        try:
            command_args = prefix + _split_repl_line(line)
            _JSON_REQUESTED = "--json" in command_args
            cli.main(args=command_args, prog_name="cli-anything-gpt-sovits", standalone_mode=False)
        except (click.ClickException, click.exceptions.Exit) as exc:
            if isinstance(exc, click.ClickException):
                if _JSON_REQUESTED:
                    _emit_click_error(exc)
                else:
                    skin.error(exc.format_message())
        finally:
            _JSON_REQUESTED = previous_json_requested
    skin.print_goodbye()


def main():
    global _JSON_REQUESTED
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    _JSON_REQUESTED = "--json" in args
    try:
        result = cli.main(args=args, prog_name="cli-anything-gpt-sovits", standalone_mode=False)
        if isinstance(result, int) and result != 0:
            raise SystemExit(result)
    except click.ClickException as exc:
        if _JSON_REQUESTED:
            _emit_click_error(exc)
        else:
            exc.show()
        raise SystemExit(exc.exit_code)
    except click.exceptions.Exit as exc:
        raise SystemExit(exc.exit_code)


if __name__ == "__main__":
    main()
