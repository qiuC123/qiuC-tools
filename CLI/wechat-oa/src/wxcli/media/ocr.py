"""Bounded replaceable local OCR with a Windows Runtime implementation."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import tempfile
import unicodedata
from collections.abc import Callable, Sequence
from io import BytesIO
from pathlib import Path
from typing import Protocol, cast

from PIL import Image, ImageOps

from wxcli.media.downloader import DownloadedMedia
from wxcli.media.models import (
    MAX_IMAGE_BYTES,
    MAX_IMAGE_PIXELS,
    MAX_OCR_CHARACTERS_PER_IMAGE,
    OCREvidence,
    OCRStatus,
)

WINDOWS_OCR_ANALYZER = "windows-media-ocr"
WINDOWS_OCR_BRIDGE_VERSION = "1"
WINDOWS_OCR_ANALYZER_VERSION = (
    f"bridge-{WINDOWS_OCR_BRIDGE_VERSION};windows-{platform.version()}"
)
OCR_TILE_HEIGHT = 2_200
OCR_TILE_OVERLAP = 120
OCR_MAX_WIDTH = 2_400
OCR_MAX_TILES = 64
OCR_RETRY_MIN_CHARACTERS = 4
OCR_ATTEMPT_TIMEOUT_SECONDS = 30.0


class OCRProvider(Protocol):
    """Replaceable OCR boundary consumed by later analysis orchestration."""

    def analyze(
        self,
        media: DownloadedMedia,
        *,
        requested_language: str = "zh-Hans",
    ) -> OCREvidence: ...


class OCRRuntime(Protocol):
    """Minimum local engine interface used by the Windows OCR provider."""

    def recognize(
        self,
        images: Sequence[Image.Image],
        *,
        language: str,
    ) -> Sequence[str]: ...


class OCRUnavailableError(RuntimeError):
    """The requested local OCR engine or language is not installed."""


class OCRExecutionError(RuntimeError):
    """The local OCR engine failed without exposing internal diagnostics."""


RunCommand = Callable[..., subprocess.CompletedProcess[str]]


class WindowsPowerShellOCRRuntime:
    """Bridge validated local image files to the built-in Windows OCR runtime."""

    def __init__(
        self,
        *,
        powershell_path: str | None = None,
        timeout_seconds: float = OCR_ATTEMPT_TIMEOUT_SECONDS,
        run_command: RunCommand = subprocess.run,
    ) -> None:
        if not 0 < timeout_seconds <= OCR_ATTEMPT_TIMEOUT_SECONDS:
            raise ValueError(
                "timeout_seconds must be greater than zero and at most 30 seconds."
            )
        self._powershell_path = powershell_path or _default_powershell_path()
        self._timeout_seconds = timeout_seconds
        self._run_command = run_command

    def recognize(
        self,
        images: Sequence[Image.Image],
        *,
        language: str,
    ) -> tuple[str, ...]:
        if not self._powershell_path or os.name != "nt":
            raise OCRUnavailableError("Windows local OCR is unavailable.")
        if not images:
            raise OCRExecutionError("Windows local OCR received no image tiles.")
        payload: list[dict[str, object]] = []
        try:
            with tempfile.TemporaryDirectory(prefix="wechat-oa-ocr-") as temporary:
                root = Path(temporary)
                for index, image in enumerate(images):
                    path = root / f"tile-{index:03d}.png"
                    image.save(path, format="PNG")
                    payload.append({"index": index, "path": str(path)})
                completed = self._invoke(payload, language=language)
        except OCRUnavailableError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise OCRExecutionError("Windows local OCR could not process the image.") from error

        try:
            response_bytes = base64.b64decode(completed.stdout.strip(), validate=True)
            response = json.loads(response_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, TypeError) as error:
            raise OCRExecutionError("Windows local OCR returned an invalid response.") from error
        if completed.returncode == 3 or response.get("available") is False:
            raise OCRUnavailableError("The requested Windows OCR language is unavailable.")
        if completed.returncode != 0 or response.get("ok") is not True:
            raise OCRExecutionError("Windows local OCR failed.")
        texts = response.get("texts")
        if not isinstance(texts, list) or len(texts) != len(images):
            raise OCRExecutionError("Windows local OCR returned an incomplete response.")
        if not all(isinstance(value, str) for value in texts):
            raise OCRExecutionError("Windows local OCR returned non-text output.")
        return tuple(cast(list[str], texts))

    def _invoke(
        self,
        payload: Sequence[dict[str, object]],
        *,
        language: str,
    ) -> subprocess.CompletedProcess[str]:
        assert self._powershell_path is not None
        encoded_script = base64.b64encode(
            _WINDOWS_OCR_SCRIPT.encode("utf-16-le")
        ).decode("ascii")
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            return self._run_command(
                [
                    self._powershell_path,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-EncodedCommand",
                    encoded_script,
                ],
                input=json.dumps(
                    {"language": language, "items": payload},
                    ensure_ascii=False,
                ),
                capture_output=True,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout_seconds,
                creationflags=creation_flags,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise OCRExecutionError("Windows local OCR process failed.") from error


class WindowsOCRProvider:
    """Analyze validated bytes locally and return bounded OCR Evidence."""

    def __init__(
        self,
        runtime: OCRRuntime | None = None,
        *,
        analyzer_version: str = WINDOWS_OCR_ANALYZER_VERSION,
        max_characters: int = MAX_OCR_CHARACTERS_PER_IMAGE,
    ) -> None:
        if not analyzer_version:
            raise ValueError("analyzer_version cannot be empty.")
        if not 1 <= max_characters <= MAX_OCR_CHARACTERS_PER_IMAGE:
            raise ValueError(
                "max_characters must be between 1 and 50000 characters."
            )
        self._runtime = runtime or WindowsPowerShellOCRRuntime()
        self._analyzer_version = analyzer_version
        self._max_characters = max_characters

    def analyze(
        self,
        media: DownloadedMedia,
        *,
        requested_language: str = "zh-Hans",
    ) -> OCREvidence:
        """Run at most two local attempts and never repair or infer OCR text."""
        if not requested_language or len(requested_language) > 64:
            raise ValueError("requested_language must contain between 1 and 64 characters.")
        try:
            image = _load_validated_image(media)
            tiles, preprocessing = _prepare_tiles(image)
            first_text = _recognize_tiles(
                self._runtime,
                tiles,
                language=requested_language,
            )
        except OCRUnavailableError:
            return self._outcome(media, requested_language, OCRStatus.UNAVAILABLE)
        except (OCRExecutionError, OSError, RuntimeError, TypeError, ValueError):
            return self._outcome(media, requested_language, OCRStatus.FAILED)

        selected_text = first_text
        selected_preprocessing = preprocessing
        if _visible_characters(first_text) < OCR_RETRY_MIN_CHARACTERS:
            try:
                enhanced, enhancement_steps = _enhance_image(image)
                enhanced_tiles, enhanced_preparation = _prepare_tiles(enhanced)
                retry_text = _recognize_tiles(
                    self._runtime,
                    enhanced_tiles,
                    language=requested_language,
                )
                if _visible_characters(retry_text) > _visible_characters(first_text):
                    selected_text = retry_text
                    selected_preprocessing = enhancement_steps + enhanced_preparation
            except (
                OCRUnavailableError,
                OCRExecutionError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
            ):
                pass

        normalized = _normalize_text(selected_text)
        truncated = len(normalized) > self._max_characters
        if truncated:
            normalized = normalized[: self._max_characters]
        return OCREvidence(
            source_byte_sha256=media.byte_sha256,
            analyzer=WINDOWS_OCR_ANALYZER,
            analyzer_version=self._analyzer_version,
            status=OCRStatus.ANALYZED,
            requested_language=requested_language,
            text=normalized,
            truncated=truncated,
            preprocessing=selected_preprocessing,
        )

    def _outcome(
        self,
        media: DownloadedMedia,
        requested_language: str,
        status: OCRStatus,
    ) -> OCREvidence:
        return OCREvidence(
            source_byte_sha256=media.byte_sha256,
            analyzer=WINDOWS_OCR_ANALYZER,
            analyzer_version=self._analyzer_version,
            status=status,
            requested_language=requested_language,
        )


def _load_validated_image(media: DownloadedMedia) -> Image.Image:
    if (
        media.byte_length != len(media.content)
        or not 1 <= media.byte_length <= MAX_IMAGE_BYTES
    ):
        raise ValueError("OCR input violates the validated byte boundary.")
    if hashlib.sha256(media.content).hexdigest() != media.byte_sha256:
        raise ValueError("OCR input bytes do not match their SHA-256.")
    with Image.open(BytesIO(media.content)) as source:
        source.seek(0)
        width, height = source.size
        if (width, height) != (media.width, media.height):
            raise ValueError("OCR input dimensions do not match validated observations.")
        if width * height > MAX_IMAGE_PIXELS:
            raise ValueError("OCR input exceeds the safe pixel limit.")
        source.load()
        return cast(Image.Image, source.convert("RGB"))


def _prepare_tiles(
    image: Image.Image,
) -> tuple[tuple[Image.Image, ...], tuple[str, ...]]:
    preprocessing: list[str] = []
    prepared = image
    if prepared.width > OCR_MAX_WIDTH:
        height = max(1, round(prepared.height * OCR_MAX_WIDTH / prepared.width))
        prepared = prepared.resize(
            (OCR_MAX_WIDTH, height),
            Image.Resampling.LANCZOS,
        )
        preprocessing.append(f"resize-width:{OCR_MAX_WIDTH}")
    if prepared.height <= OCR_TILE_HEIGHT:
        return (prepared,), tuple(preprocessing)

    preprocessing.append(f"tile-height:{OCR_TILE_HEIGHT}")
    preprocessing.append(f"tile-overlap:{OCR_TILE_OVERLAP}")
    tiles: list[Image.Image] = []
    top = 0
    while top < prepared.height:
        bottom = min(prepared.height, top + OCR_TILE_HEIGHT)
        tiles.append(prepared.crop((0, top, prepared.width, bottom)))
        if len(tiles) > OCR_MAX_TILES:
            raise ValueError("OCR input exceeds the tile-count limit.")
        if bottom >= prepared.height:
            break
        top = bottom - OCR_TILE_OVERLAP
    return tuple(tiles), tuple(preprocessing)


def _enhance_image(image: Image.Image) -> tuple[Image.Image, tuple[str, ...]]:
    maximum_scale = min(
        2.0,
        OCR_MAX_WIDTH / image.width,
        math.sqrt(MAX_IMAGE_PIXELS / (image.width * image.height)),
    )
    steps: list[str] = []
    enhanced = image
    if maximum_scale >= 1.1:
        width = max(1, round(image.width * maximum_scale))
        height = max(1, round(image.height * maximum_scale))
        enhanced = image.resize((width, height), Image.Resampling.LANCZOS)
        steps.append(f"upscale:{maximum_scale:.2f}x")
    enhanced = ImageOps.autocontrast(ImageOps.grayscale(enhanced)).convert("RGB")
    steps.extend(("grayscale", "autocontrast"))
    return enhanced, tuple(steps)


def _recognize_tiles(
    runtime: OCRRuntime,
    tiles: Sequence[Image.Image],
    *,
    language: str,
) -> str:
    values = runtime.recognize(tiles, language=language)
    if len(values) != len(tiles) or not all(isinstance(value, str) for value in values):
        raise OCRExecutionError("The OCR runtime returned an incomplete result.")
    return _merge_tile_text(values)


def _merge_tile_text(values: Sequence[str]) -> str:
    merged: list[str] = []
    for value in values:
        lines = _normalize_text(value).strip().splitlines()
        if not lines:
            continue
        overlap = 0
        for count in range(min(20, len(merged), len(lines)), 0, -1):
            if merged[-count:] == lines[:count]:
                overlap = count
                break
        merged.extend(lines[overlap:])
    return "\n".join(merged)


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize(
        "NFC",
        value.replace("\r\n", "\n").replace("\r", "\n"),
    )
    return "".join(
        character
        for character in normalized
        if character in {"\n", "\t"} or unicodedata.category(character) != "Cc"
    )


def _visible_characters(value: str) -> int:
    return sum(not character.isspace() for character in value)


def _default_powershell_path() -> str | None:
    if os.name != "nt":
        return None
    if system_root := os.environ.get("SystemRoot"):
        candidate = (
            Path(system_root)
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        if candidate.is_file():
            return str(candidate)
    return shutil.which("powershell.exe")


_WINDOWS_OCR_SCRIPT = r"""
$ErrorActionPreference = "Stop"
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
function Await-Operation {
    param($Operation, [Type]$ResultType)
    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() |
        Where-Object {
            $_.Name -eq "AsTask" -and $_.IsGenericMethod -and
            $_.GetParameters().Count -eq 1
        } | Select-Object -First 1
    $task = $method.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    $task.Wait()
    return $task.Result
}
function Write-Response {
    param($Value)
    $json = ConvertTo-Json -InputObject $Value -Depth 3 -Compress
    [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($json))
}
$inputJson = [Console]::In.ReadToEnd().TrimStart([char]0xFEFF)
$request = $inputJson | ConvertFrom-Json
$language = [Windows.Globalization.Language]::new([string]$request.language)
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
if ($null -eq $engine) {
    Write-Response @{available = $false; ok = $false}
    exit 3
}
$items = $request.items
$remainingCharacters = 50001
$texts = foreach ($item in $items) {
    $stream = $null
    $bitmap = $null
    try {
        $file = Await-Operation ([Windows.Storage.StorageFile]::GetFileFromPathAsync([string]$item.path)) ([Windows.Storage.StorageFile])
        $stream = Await-Operation ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Await-Operation ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await-Operation ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $recognized = Await-Operation ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        $text = [string]$recognized.Text
        if ($text.Length -gt $remainingCharacters) {
            $text = $text.Substring(0, $remainingCharacters)
        }
        $remainingCharacters -= $text.Length
        $text
    }
    finally {
        if ($null -ne $bitmap) { $bitmap.Dispose() }
        if ($null -ne $stream) { $stream.Dispose() }
    }
}
Write-Response @{available = $true; ok = $true; texts = @($texts)}
"""
