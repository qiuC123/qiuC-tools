"""Offline capability diagnostics for optional local media analysis."""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Callable
from enum import StrEnum
from io import BytesIO
from typing import Literal, Protocol

import zxingcpp
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, model_validator

from wxcli.media.downloader import DownloadedMedia
from wxcli.media.models import MediaFormat, QRStatus
from wxcli.media.ocr import (
    OCRExecutionError,
    OCRRuntimeCapabilities,
    OCRUnavailableError,
    WINDOWS_OCR_ANALYZER,
    WINDOWS_OCR_ANALYZER_VERSION,
    WindowsPowerShellOCRRuntime,
)
from wxcli.media.qr import (
    STANDARD_QR_ANALYZER,
    STANDARD_QR_ANALYZER_VERSION,
    StandardQRAnalyzer,
)

MEDIA_DOCTOR_SCHEMA_VERSION: Literal["1"] = "1"
DEFAULT_OCR_LANGUAGE = "zh-Hans"
_QR_PROBE_PAYLOAD = "wechat-oa-media-doctor"


class OCRCapabilityAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class ImageDecoderCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    jpeg: bool
    png: bool
    webp: bool
    gif: bool

    @property
    def ready(self) -> bool:
        return self.jpeg and self.png and self.webp and self.gif


class StandardQRCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    available: bool
    analyzer: str = STANDARD_QR_ANALYZER
    analyzer_version: str = STANDARD_QR_ANALYZER_VERSION


class WindowsOCRCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    availability: OCRCapabilityAvailability
    analyzer: str = WINDOWS_OCR_ANALYZER
    analyzer_version: str = WINDOWS_OCR_ANALYZER_VERSION
    default_language: str = DEFAULT_OCR_LANGUAGE
    default_language_available: bool = False
    languages: tuple[str, ...] = Field(default_factory=tuple, max_length=100)

    @model_validator(mode="after")
    def validate_outcome(self) -> WindowsOCRCapability:
        if self.availability != OCRCapabilityAvailability.AVAILABLE and (
            self.languages or self.default_language_available
        ):
            raise ValueError("Unavailable or failed OCR capability cannot list languages.")
        if self.languages != tuple(sorted(set(self.languages), key=str.casefold)):
            raise ValueError("OCR capability languages must be unique and stably ordered.")
        if any(
            not language
            or len(language) > 64
            or any(unicodedata.category(character) == "Cc" for character in language)
            for language in self.languages
        ):
            raise ValueError("OCR capability languages must be bounded printable tags.")
        return self


class MediaDoctorReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = MEDIA_DOCTOR_SCHEMA_VERSION
    overall: Literal["pass", "fail"]
    image_decoders: ImageDecoderCapabilities
    standard_qr: StandardQRCapability
    windows_ocr: WindowsOCRCapability

    @model_validator(mode="after")
    def validate_overall(self) -> MediaDoctorReport:
        expected = (
            "pass"
            if self.image_decoders.ready and self.standard_qr.available
            else "fail"
        )
        if self.overall != expected:
            raise ValueError("Media Doctor overall result does not match required capabilities.")
        return self


class OCRCapabilityRuntime(Protocol):
    def capabilities(
        self,
        *,
        default_language: str = DEFAULT_OCR_LANGUAGE,
    ) -> OCRRuntimeCapabilities: ...


ImageDecoderProbe = Callable[[], ImageDecoderCapabilities]
StandardQRProbe = Callable[[], bool]


class MediaDoctor:
    """Probe only local packaged and operating-system media capabilities."""

    def __init__(
        self,
        *,
        image_decoder_probe: ImageDecoderProbe | None = None,
        standard_qr_probe: StandardQRProbe | None = None,
        ocr_runtime: OCRCapabilityRuntime | None = None,
    ) -> None:
        self._image_decoder_probe = image_decoder_probe or _pillow_decoders
        self._standard_qr_probe = standard_qr_probe or _standard_qr_round_trip
        self._ocr_runtime = ocr_runtime or WindowsPowerShellOCRRuntime(
            timeout_seconds=10.0
        )

    def run(self) -> MediaDoctorReport:
        try:
            image_decoders = self._image_decoder_probe()
        except Exception:
            image_decoders = ImageDecoderCapabilities(
                jpeg=False,
                png=False,
                webp=False,
                gif=False,
            )
        try:
            standard_qr_available = bool(self._standard_qr_probe())
        except Exception:
            standard_qr_available = False
        windows_ocr = self._windows_ocr()
        overall: Literal["pass", "fail"] = (
            "pass" if image_decoders.ready and standard_qr_available else "fail"
        )
        return MediaDoctorReport(
            overall=overall,
            image_decoders=image_decoders,
            standard_qr=StandardQRCapability(available=standard_qr_available),
            windows_ocr=windows_ocr,
        )

    def _windows_ocr(self) -> WindowsOCRCapability:
        try:
            capabilities = self._ocr_runtime.capabilities(
                default_language=DEFAULT_OCR_LANGUAGE
            )
            return WindowsOCRCapability(
                availability=OCRCapabilityAvailability.AVAILABLE,
                default_language_available=capabilities.default_language_available,
                languages=capabilities.languages,
            )
        except OCRUnavailableError:
            return WindowsOCRCapability(
                availability=OCRCapabilityAvailability.UNAVAILABLE
            )
        except (OCRExecutionError, OSError, RuntimeError, TypeError, ValueError):
            return WindowsOCRCapability(
                availability=OCRCapabilityAvailability.FAILED
            )


def _pillow_decoders() -> ImageDecoderCapabilities:
    formats = set(Image.registered_extensions().values())
    return ImageDecoderCapabilities(
        jpeg="JPEG" in formats,
        png="PNG" in formats,
        webp="WEBP" in formats,
        gif="GIF" in formats,
    )


def _standard_qr_round_trip() -> bool:
    barcode = zxingcpp.create_barcode(
        _QR_PROBE_PAYLOAD,
        zxingcpp.BarcodeFormat.QRCode,
    )
    image = Image.fromarray(barcode.to_image(scale=5))
    output = BytesIO()
    image.save(output, format="PNG")
    content = output.getvalue()
    media = DownloadedMedia(
        source_url="https://mmbiz.qpic.cn/local-doctor/qr.png",
        final_url="https://mmbiz.qpic.cn/local-doctor/qr.png",
        content=content,
        byte_sha256=hashlib.sha256(content).hexdigest(),
        media_format=MediaFormat.PNG,
        media_type="image/png",
        byte_length=len(content),
        width=image.width,
        height=image.height,
        redirect_urls=(),
    )
    evidence = StandardQRAnalyzer().analyze(media)
    return (
        evidence.status == QRStatus.DECODED
        and len(evidence.payloads) == 1
        and evidence.payloads[0].payload == _QR_PROBE_PAYLOAD
    )
