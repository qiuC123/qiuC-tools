from __future__ import annotations

import pytest
from pydantic import ValidationError

from wxcli.media import (
    ImageDecoderCapabilities,
    MediaDoctor,
    MediaDoctorReport,
    OCRCapabilityAvailability,
    OCRExecutionError,
    OCRRuntimeCapabilities,
    OCRUnavailableError,
    StandardQRCapability,
    WindowsOCRCapability,
)


class FakeOCRRuntime:
    def __init__(self, result: OCRRuntimeCapabilities | Exception) -> None:
        self.result = result
        self.languages: list[str] = []

    def capabilities(
        self,
        *,
        default_language: str = "zh-Hans",
    ) -> OCRRuntimeCapabilities:
        self.languages.append(default_language)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def all_decoders() -> ImageDecoderCapabilities:
    return ImageDecoderCapabilities(jpeg=True, png=True, webp=True, gif=True)


def test_media_doctor_runs_real_packaged_decoder_and_qr_probes_offline() -> None:
    runtime = FakeOCRRuntime(
        OCRRuntimeCapabilities(
            languages=("en-US", "zh-Hans-CN"),
            default_language_available=True,
        )
    )

    report = MediaDoctor(ocr_runtime=runtime).run()

    assert report.schema_version == "1"
    assert report.overall == "pass"
    assert report.image_decoders.ready is True
    assert report.standard_qr.available is True
    assert report.windows_ocr.availability == OCRCapabilityAvailability.AVAILABLE
    assert report.windows_ocr.languages == ("en-US", "zh-Hans-CN")
    assert report.windows_ocr.default_language_available is True
    assert runtime.languages == ["zh-Hans"]


@pytest.mark.parametrize(
    ("error", "availability"),
    [
        (OCRUnavailableError("missing"), OCRCapabilityAvailability.UNAVAILABLE),
        (OCRExecutionError("failed"), OCRCapabilityAvailability.FAILED),
    ],
)
def test_optional_windows_ocr_does_not_fail_required_media_capabilities(
    error: Exception,
    availability: OCRCapabilityAvailability,
) -> None:
    report = MediaDoctor(
        image_decoder_probe=all_decoders,
        standard_qr_probe=lambda: True,
        ocr_runtime=FakeOCRRuntime(error),
    ).run()

    assert report.overall == "pass"
    assert report.windows_ocr.availability == availability
    assert report.windows_ocr.languages == ()


@pytest.mark.parametrize(
    ("decoders", "qr_available"),
    [
        (ImageDecoderCapabilities(jpeg=True, png=True, webp=False, gif=True), True),
        (all_decoders(), False),
    ],
)
def test_missing_required_decoder_or_qr_capability_fails_media_doctor(
    decoders: ImageDecoderCapabilities,
    qr_available: bool,
) -> None:
    report = MediaDoctor(
        image_decoder_probe=lambda: decoders,
        standard_qr_probe=lambda: qr_available,
        ocr_runtime=FakeOCRRuntime(OCRUnavailableError("missing")),
    ).run()

    assert report.overall == "fail"


def test_probe_exceptions_and_malformed_ocr_languages_degrade_safely() -> None:
    def broken_decoders() -> ImageDecoderCapabilities:
        raise RuntimeError("private decoder detail")

    report = MediaDoctor(
        image_decoder_probe=broken_decoders,
        standard_qr_probe=lambda: (_ for _ in ()).throw(
            RuntimeError("private QR detail")
        ),
        ocr_runtime=FakeOCRRuntime(
            OCRRuntimeCapabilities(
                languages=("bad\nvalue",),
                default_language_available=True,
            )
        ),
    ).run()

    assert report.overall == "fail"
    assert report.image_decoders.ready is False
    assert report.standard_qr.available is False
    assert report.windows_ocr.availability == OCRCapabilityAvailability.FAILED


def test_media_doctor_models_reject_inconsistent_outcomes() -> None:
    with pytest.raises(ValidationError, match="overall"):
        MediaDoctorReport(
            overall="pass",
            image_decoders=ImageDecoderCapabilities(
                jpeg=True,
                png=True,
                webp=False,
                gif=True,
            ),
            standard_qr=StandardQRCapability(available=True),
            windows_ocr=WindowsOCRCapability(
                availability=OCRCapabilityAvailability.UNAVAILABLE
            ),
        )
