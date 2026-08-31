from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import replace
from io import BytesIO

import pytest
import zxingcpp
from PIL import Image

from wxcli.media import (
    DownloadedMedia,
    MediaFormat,
    QRPayloadType,
    QRStatus,
    StandardQRAnalyzer,
)


class FakeDecoder:
    def __init__(self, payloads: Sequence[str] = (), *, error: Exception | None = None) -> None:
        self.payloads = tuple(payloads)
        self.error = error
        self.max_symbols: int | None = None

    def decode(self, image: Image.Image, *, max_symbols: int) -> Sequence[str]:
        assert image.mode == "RGB"
        self.max_symbols = max_symbols
        if self.error is not None:
            raise self.error
        return self.payloads[:max_symbols]


def media_from_bytes(content: bytes) -> DownloadedMedia:
    with Image.open(BytesIO(content)) as image:
        width, height = image.size
    return DownloadedMedia(
        source_url="https://mmbiz.qpic.cn/example/qr.png",
        final_url="https://mmbiz.qpic.cn/example/qr.png",
        content=content,
        byte_sha256=hashlib.sha256(content).hexdigest(),
        media_format=MediaFormat.PNG,
        media_type="image/png",
        byte_length=len(content),
        width=width,
        height=height,
        redirect_urls=(),
    )


def blank_png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 32), "white").save(output, format="PNG")
    return output.getvalue()


def generated_qr_png(payload: str) -> bytes:
    barcode = zxingcpp.create_barcode(payload, zxingcpp.BarcodeFormat.QRCode)
    image = Image.fromarray(barcode.to_image(scale=5))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_packaged_decoder_reads_standard_qr_without_visiting_url() -> None:
    payload = "https://jobs.example.test/apply?id=42"

    evidence = StandardQRAnalyzer().analyze(media_from_bytes(generated_qr_png(payload)))

    assert evidence.status == QRStatus.DECODED
    assert evidence.payloads[0].payload == payload
    assert evidence.payloads[0].payload_type == QRPayloadType.URL
    assert evidence.payloads[0].payload_sha256 == hashlib.sha256(payload.encode()).hexdigest()


def test_no_qr_is_a_successful_not_found_outcome() -> None:
    evidence = StandardQRAnalyzer().analyze(media_from_bytes(blank_png()))

    assert evidence.status == QRStatus.NOT_FOUND
    assert evidence.payloads == ()


def test_payloads_keep_decoder_order_and_are_classified_as_inert_data() -> None:
    decoder = FakeDecoder(
        [
            "plain recruitment text",
            "BEGIN:VCARD\nFN:Campus HR\nEND:VCARD",
            "https://example.test/apply",
        ]
    )

    evidence = StandardQRAnalyzer(decoder, analyzer_version="test-engine").analyze(
        media_from_bytes(blank_png())
    )

    assert decoder.max_symbols == 21
    assert [item.index for item in evidence.payloads] == [0, 1, 2]
    assert [item.payload_type for item in evidence.payloads] == [
        QRPayloadType.TEXT,
        QRPayloadType.CONTACT,
        QRPayloadType.URL,
    ]


def test_terminal_controls_are_replaced_before_evidence_is_created() -> None:
    evidence = StandardQRAnalyzer(
        FakeDecoder(["safe\x1b[31m\x9btext\nnext"]), analyzer_version="test-engine"
    ).analyze(media_from_bytes(blank_png()))

    assert evidence.status == QRStatus.DECODED
    assert evidence.payloads[0].payload == "safe\ufffd[31m\ufffdtext\ufffdnext"
    assert "\x1b" not in evidence.payloads[0].payload


@pytest.mark.parametrize(
    "decoder",
    [
        FakeDecoder(["x"] * 21),
        FakeDecoder(["x" * 4097]),
        FakeDecoder(error=RuntimeError("synthetic decoder failure")),
    ],
)
def test_decoder_failures_and_output_overflow_return_no_partial_payloads(
    decoder: FakeDecoder,
) -> None:
    evidence = StandardQRAnalyzer(decoder, analyzer_version="test-engine").analyze(
        media_from_bytes(blank_png())
    )

    assert evidence.status == QRStatus.FAILED
    assert evidence.payloads == ()


def test_tampered_bytes_or_observations_are_rejected_before_decode() -> None:
    decoder = FakeDecoder(["must not be reached"])
    media = media_from_bytes(blank_png())
    tampered = replace(media, byte_sha256="f" * 64)

    evidence = StandardQRAnalyzer(decoder, analyzer_version="test-engine").analyze(tampered)

    assert evidence.status == QRStatus.FAILED
    assert decoder.max_symbols is None


def test_configured_limits_may_only_tighten_hard_bounds() -> None:
    with pytest.raises(ValueError, match="max_payloads"):
        StandardQRAnalyzer(max_payloads=21)
    with pytest.raises(ValueError, match="max_payload_bytes"):
        StandardQRAnalyzer(max_payload_bytes=4097)
