from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from collections.abc import Sequence
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from wxcli.media import (
    DownloadedMedia,
    MediaFormat,
    OCRExecutionError,
    OCRRuntimeCapabilities,
    OCRStatus,
    OCRUnavailableError,
    WindowsOCRProvider,
    WindowsPowerShellOCRRuntime,
)


class FakeRuntime:
    def __init__(self, *responses: Sequence[str] | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[tuple[Image.Image, ...], str]] = []

    def recognize(
        self,
        images: Sequence[Image.Image],
        *,
        language: str,
    ) -> Sequence[str]:
        self.calls.append((tuple(images), language))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def media_image(width: int = 120, height: int = 80) -> DownloadedMedia:
    output = BytesIO()
    Image.new("RGB", (width, height), "white").save(output, format="PNG")
    content = output.getvalue()
    return DownloadedMedia(
        source_url="https://mmbiz.qpic.cn/example/poster.png",
        final_url="https://mmbiz.qpic.cn/example/poster.png",
        content=content,
        byte_sha256=hashlib.sha256(content).hexdigest(),
        media_format=MediaFormat.PNG,
        media_type="image/png",
        byte_length=len(content),
        width=width,
        height=height,
        redirect_urls=(),
    )


def test_windows_provider_returns_normalized_raw_text_without_rewriting() -> None:
    runtime = FakeRuntime(["AI\r\nModeI\x1b[31m"])

    evidence = WindowsOCRProvider(runtime, analyzer_version="test-runtime").analyze(
        media_image(),
        requested_language="zh-Hans",
    )

    assert evidence.status == OCRStatus.ANALYZED
    assert evidence.text == "AI\nModeI[31m"
    assert evidence.requested_language == "zh-Hans"
    assert evidence.detected_language is None
    assert evidence.confidence is None
    assert evidence.preprocessing == ()
    assert runtime.calls[0][1] == "zh-Hans"
    assert len(runtime.calls) == 1


def test_sparse_text_retries_with_bounded_deterministic_enhancement() -> None:
    runtime = FakeRuntime(["导师"], ["成长导师\n腾讯科学家"])

    evidence = WindowsOCRProvider(runtime, analyzer_version="test-runtime").analyze(
        media_image(600, 400)
    )

    assert evidence.status == OCRStatus.ANALYZED
    assert evidence.text == "成长导师\n腾讯科学家"
    assert evidence.preprocessing == (
        "upscale:2.00x",
        "grayscale",
        "autocontrast",
    )
    assert len(runtime.calls) == 2
    retry_image = runtime.calls[1][0][0]
    assert retry_image.mode == "RGB"
    assert retry_image.size == (1200, 800)


def test_failed_or_worse_retry_preserves_successful_first_observation() -> None:
    failed_retry = FakeRuntime([""], OCRExecutionError("synthetic retry failure"))
    failed_evidence = WindowsOCRProvider(
        failed_retry,
        analyzer_version="test-runtime",
    ).analyze(media_image())
    assert failed_evidence.status == OCRStatus.ANALYZED
    assert failed_evidence.text == ""
    assert failed_evidence.preprocessing == ()

    worse_retry = FakeRuntime(["导师"], [""])
    worse_evidence = WindowsOCRProvider(
        worse_retry,
        analyzer_version="test-runtime",
    ).analyze(media_image())
    assert worse_evidence.text == "导师"
    assert worse_evidence.preprocessing == ()


def test_long_poster_is_tiled_and_exact_overlap_lines_are_deduplicated() -> None:
    runtime = FakeRuntime(
        [
            "第一段\n重叠行",
            "重叠行\n第二段",
            "第二段\n结尾",
        ]
    )

    evidence = WindowsOCRProvider(runtime, analyzer_version="test-runtime").analyze(
        media_image(100, 4500)
    )

    assert evidence.text == "第一段\n重叠行\n第二段\n结尾"
    assert evidence.preprocessing == (
        "tile-height:2200",
        "tile-overlap:120",
    )
    assert [image.height for image in runtime.calls[0][0]] == [2200, 2200, 340]


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (OCRUnavailableError("missing language"), OCRStatus.UNAVAILABLE),
        (OCRExecutionError("runtime failed"), OCRStatus.FAILED),
    ],
)
def test_local_capability_and_runtime_failures_are_stable_outcomes(
    error: Exception,
    status: OCRStatus,
) -> None:
    evidence = WindowsOCRProvider(
        FakeRuntime(error),
        analyzer_version="test-runtime",
    ).analyze(media_image())

    assert evidence.status == status
    assert evidence.text is None
    assert evidence.preprocessing == ()


def test_tampered_media_is_rejected_before_the_runtime() -> None:
    runtime = FakeRuntime(["must not run"])
    media = replace(media_image(), byte_sha256="f" * 64)

    evidence = WindowsOCRProvider(runtime, analyzer_version="test-runtime").analyze(media)

    assert evidence.status == OCRStatus.FAILED
    assert runtime.calls == []


def test_extreme_aspect_ratio_is_rejected_before_unbounded_tile_creation() -> None:
    runtime = FakeRuntime(["must not run"])

    evidence = WindowsOCRProvider(runtime, analyzer_version="test-runtime").analyze(
        media_image(1, 140_000)
    )

    assert evidence.status == OCRStatus.FAILED
    assert runtime.calls == []


def test_text_limit_truncates_normalized_output_without_inference() -> None:
    evidence = WindowsOCRProvider(
        FakeRuntime(["abcdef"]),
        analyzer_version="test-runtime",
        max_characters=5,
    ).analyze(media_image())

    assert evidence.text == "abcde"
    assert evidence.truncated is True


def test_runtime_bridge_uses_only_temporary_tiles_and_structured_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("wxcli.media.ocr.os.name", "nt")
    observation: dict[str, object] = {}

    def run_command(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        request = json.loads(str(kwargs["input"]))
        tile_path = request["items"][0]["path"]
        observation["tile_exists"] = Path(tile_path).is_file()
        observation["language"] = request["language"]
        encoded = command[command.index("-EncodedCommand") + 1]
        observation["command"] = command
        script = base64.b64decode(encoded).decode("utf-16-le")
        observation["script"] = script
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=base64.b64encode(
                '{"available":true,"ok":true,"texts":["校园招聘"]}'.encode()
            ).decode(),
            stderr="",
        )

    runtime = WindowsPowerShellOCRRuntime(
        powershell_path="powershell.exe",
        run_command=run_command,
    )

    texts = runtime.recognize([Image.new("RGB", (20, 20))], language="zh-Hans")

    assert texts == ("校园招聘",)
    assert observation["tile_exists"] is True
    assert observation["language"] == "zh-Hans"
    assert "Windows.Media.Ocr.OcrEngine" in str(observation["script"])
    assert "http" not in str(observation["script"]).casefold()
    assert "Bypass" not in observation["command"]


def test_runtime_bridge_maps_missing_language_and_invalid_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("wxcli.media.ocr.os.name", "nt")

    def unavailable(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            3,
            stdout=base64.b64encode(
                b'{"available":false,"ok":false}'
            ).decode(),
            stderr="",
        )

    runtime = WindowsPowerShellOCRRuntime(
        powershell_path="powershell.exe",
        run_command=unavailable,
    )
    with pytest.raises(OCRUnavailableError):
        runtime.recognize([Image.new("RGB", (20, 20))], language="zh-Hans")

    def invalid(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout="not-json", stderr="")

    broken = WindowsPowerShellOCRRuntime(
        powershell_path="powershell.exe",
        run_command=invalid,
    )
    with pytest.raises(OCRExecutionError):
        broken.recognize([Image.new("RGB", (20, 20))], language="zh-Hans")


def test_runtime_capability_probe_is_bounded_local_and_stably_orders_languages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("wxcli.media.ocr.os.name", "nt")
    observation: dict[str, object] = {}

    def run_command(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observation["request"] = json.loads(str(kwargs["input"]))
        encoded = command[command.index("-EncodedCommand") + 1]
        observation["script"] = base64.b64decode(encoded).decode("utf-16-le")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=base64.b64encode(
                json.dumps(
                    {
                        "available": True,
                        "ok": True,
                        "languages": ["zh-Hans-CN", "en-US", "en-US"],
                        "defaultLanguageAvailable": True,
                    }
                ).encode()
            ).decode(),
            stderr="",
        )

    runtime = WindowsPowerShellOCRRuntime(
        powershell_path="powershell.exe",
        run_command=run_command,
    )

    result = runtime.capabilities(default_language="zh-Hans")

    assert result == OCRRuntimeCapabilities(
        languages=("en-US", "zh-Hans-CN"),
        default_language_available=True,
    )
    assert observation["request"] == {"defaultLanguage": "zh-Hans"}
    assert "AvailableRecognizerLanguages" in str(observation["script"])
    assert "http" not in str(observation["script"]).casefold()


def test_runtime_capability_probe_maps_unavailable_and_invalid_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("wxcli.media.ocr.os.name", "nt")

    def unavailable(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            3,
            stdout=base64.b64encode(b'{"available":false,"ok":false}').decode(),
            stderr="",
        )

    runtime = WindowsPowerShellOCRRuntime(
        powershell_path="powershell.exe",
        run_command=unavailable,
    )
    with pytest.raises(OCRUnavailableError):
        runtime.capabilities()

    def invalid(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=base64.b64encode(b'[]').decode(),
            stderr="",
        )

    broken = WindowsPowerShellOCRRuntime(
        powershell_path="powershell.exe",
        run_command=invalid,
    )
    with pytest.raises(OCRExecutionError):
        broken.capabilities()


def test_configuration_can_only_tighten_hard_limits() -> None:
    with pytest.raises(ValueError, match="max_characters"):
        WindowsOCRProvider(max_characters=50_001)
    with pytest.raises(ValueError, match="timeout_seconds"):
        WindowsPowerShellOCRRuntime(timeout_seconds=31)
