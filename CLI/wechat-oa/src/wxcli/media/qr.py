"""Bounded local standard-QR analysis over already validated image bytes."""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Sequence
from importlib.metadata import version
from io import BytesIO
from typing import Protocol, cast
from urllib.parse import urlsplit

import zxingcpp
from PIL import Image

from wxcli.media.downloader import DownloadedMedia
from wxcli.media.models import (
    MAX_IMAGE_BYTES,
    MAX_IMAGE_PIXELS,
    MAX_QR_PAYLOAD_BYTES,
    MAX_QR_PAYLOADS_PER_IMAGE,
    QREvidence,
    QRPayloadEvidence,
    QRPayloadType,
    QRStatus,
)

STANDARD_QR_ANALYZER = "standard-qr"
STANDARD_QR_ANALYZER_VERSION = version("zxing-cpp")


class StandardQRDecoder(Protocol):
    """Replaceable local decoder boundary used by offline tests and packaging."""

    def decode(self, image: Image.Image, *, max_symbols: int) -> Sequence[str]: ...


class ZXingStandardQRDecoder:
    """Decode only ISO/IEC standard QR symbols with the packaged local engine."""

    def decode(self, image: Image.Image, *, max_symbols: int) -> tuple[str, ...]:
        barcodes = zxingcpp.read_barcodes(
            image,
            formats=zxingcpp.BarcodeFormats(zxingcpp.BarcodeFormat.QRCode),
            text_mode=zxingcpp.TextMode.Plain,
            return_errors=False,
        )
        return tuple(barcode.text for barcode in barcodes[:max_symbols])


class StandardQRAnalyzer:
    """Produce inert QR Evidence without opening or interpreting destinations."""

    def __init__(
        self,
        decoder: StandardQRDecoder | None = None,
        *,
        analyzer_version: str = STANDARD_QR_ANALYZER_VERSION,
        max_payloads: int = MAX_QR_PAYLOADS_PER_IMAGE,
        max_payload_bytes: int = MAX_QR_PAYLOAD_BYTES,
    ) -> None:
        if not 1 <= max_payloads <= MAX_QR_PAYLOADS_PER_IMAGE:
            raise ValueError(
                f"max_payloads must be between 1 and {MAX_QR_PAYLOADS_PER_IMAGE}."
            )
        if not 1 <= max_payload_bytes <= MAX_QR_PAYLOAD_BYTES:
            raise ValueError(
                f"max_payload_bytes must be between 1 and {MAX_QR_PAYLOAD_BYTES}."
            )
        if not analyzer_version:
            raise ValueError("analyzer_version cannot be empty.")
        self._decoder = decoder or ZXingStandardQRDecoder()
        self._analyzer_version = analyzer_version
        self._max_payloads = max_payloads
        self._max_payload_bytes = max_payload_bytes

    def analyze(self, media: DownloadedMedia) -> QREvidence:
        """Analyze one validated image and always return a bounded stable outcome."""
        try:
            image = self._load_image(media)
            decoded = tuple(
                self._decoder.decode(image, max_symbols=self._max_payloads + 1)
            )
            if len(decoded) > self._max_payloads:
                return self._failed(media)
            payloads = tuple(
                self._payload(index, payload) for index, payload in enumerate(decoded)
            )
        except (OSError, RuntimeError, TypeError, ValueError, UnicodeError):
            return self._failed(media)

        if not payloads:
            return QREvidence(
                source_byte_sha256=media.byte_sha256,
                analyzer=STANDARD_QR_ANALYZER,
                analyzer_version=self._analyzer_version,
                status=QRStatus.NOT_FOUND,
            )
        return QREvidence(
            source_byte_sha256=media.byte_sha256,
            analyzer=STANDARD_QR_ANALYZER,
            analyzer_version=self._analyzer_version,
            status=QRStatus.DECODED,
            payloads=payloads,
        )

    def _load_image(self, media: DownloadedMedia) -> Image.Image:
        if media.byte_length != len(media.content) or not 1 <= media.byte_length <= MAX_IMAGE_BYTES:
            raise ValueError("QR input violates the validated byte boundary.")
        if hashlib.sha256(media.content).hexdigest() != media.byte_sha256:
            raise ValueError("QR input bytes do not match their SHA-256.")
        with Image.open(BytesIO(media.content)) as source:
            source.seek(0)
            width, height = source.size
            if (width, height) != (media.width, media.height):
                raise ValueError("QR input dimensions do not match validated observations.")
            if width * height > MAX_IMAGE_PIXELS:
                raise ValueError("QR input exceeds the safe pixel limit.")
            source.load()
            return cast(Image.Image, source.convert("RGB"))

    def _payload(self, index: int, raw_payload: str) -> QRPayloadEvidence:
        if not isinstance(raw_payload, str):
            raise TypeError("The QR decoder returned a non-text payload.")
        payload = _sanitize_terminal_text(raw_payload)
        if len(payload.encode("utf-8")) > self._max_payload_bytes:
            raise ValueError("The QR payload exceeds the configured byte limit.")
        return QRPayloadEvidence.from_payload(
            index=index,
            payload_type=_payload_type(payload),
            payload=payload,
        )

    def _failed(self, media: DownloadedMedia) -> QREvidence:
        return QREvidence(
            source_byte_sha256=media.byte_sha256,
            analyzer=STANDARD_QR_ANALYZER,
            analyzer_version=self._analyzer_version,
            status=QRStatus.FAILED,
        )


def _sanitize_terminal_text(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    return "".join(
        "\ufffd" if unicodedata.category(character) == "Cc" else character
        for character in normalized
    )


def _payload_type(payload: str) -> QRPayloadType:
    lowered = payload.casefold()
    parsed = urlsplit(payload)
    if parsed.scheme.casefold() in {"http", "https"} and parsed.netloc:
        return QRPayloadType.URL
    if lowered.startswith(("begin:vcard", "mecard:", "mailto:", "tel:", "sms:", "smsto:")):
        return QRPayloadType.CONTACT
    if payload:
        return QRPayloadType.TEXT
    return QRPayloadType.UNKNOWN
