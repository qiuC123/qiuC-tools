from __future__ import annotations

from collections.abc import Iterator
from io import BytesIO
import socket

import httpx
import pytest
from PIL import Image, features

from wxcli.media import MediaDownloader, MediaDownloadFailure, MediaFormat, MediaItemReason
from wxcli.media.models import MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS

IMAGE_URL = "https://mmbiz.qpic.cn/mmbiz_png/example/640?from=appmsg#fragment"
PUBLIC_V4 = "8.8.8.8"


class StubResolver:
    def __init__(self, *addresses: str) -> None:
        self.addresses = addresses
        self.calls: list[tuple[str, int]] = []

    def resolve(self, host: str, port: int) -> tuple[str, ...]:
        self.calls.append((host, port))
        return self.addresses


class FailingResolver:
    def __init__(self) -> None:
        self.calls = 0

    def resolve(self, host: str, port: int) -> tuple[str, ...]:
        self.calls += 1
        raise socket.gaierror("offline")


class ChunkedBody(httpx.SyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks

    def __iter__(self) -> Iterator[bytes]:
        yield from self.chunks


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def image_bytes(
    media_format: str = "PNG",
    *,
    size: tuple[int, int] = (3, 2),
    frames: int = 1,
) -> bytes:
    output = BytesIO()
    images = [Image.new("RGB", size, (index * 40, 80, 120)) for index in range(frames)]
    images[0].save(
        output,
        format=media_format,
        save_all=frames > 1,
        append_images=images[1:],
    )
    return output.getvalue()


def downloader(
    handler: httpx.MockTransport | httpx.BaseTransport,
    *,
    resolver: StubResolver | FailingResolver | None = None,
    **options: object,
) -> MediaDownloader:
    return MediaDownloader(
        resolver=resolver or StubResolver(PUBLIC_V4),
        transport=handler,
        **options,
    )


@pytest.mark.parametrize(
    ("pillow_format", "content_type", "expected"),
    [
        ("JPEG", "image/jpeg", MediaFormat.JPEG),
        ("PNG", "image/png; charset=binary", MediaFormat.PNG),
        ("GIF", "image/gif", MediaFormat.GIF),
    ],
)
def test_download_validates_supported_raster_bytes(
    pillow_format: str,
    content_type: str,
    expected: MediaFormat,
) -> None:
    content = image_bytes(pillow_format)
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, content=content, headers={"Content-Type": content_type})
    )

    downloaded = downloader(transport).download(IMAGE_URL)

    assert downloaded.source_url == IMAGE_URL
    assert downloaded.final_url == IMAGE_URL.removesuffix("#fragment")
    assert downloaded.content == content
    assert downloaded.media_format == expected
    assert downloaded.media_type == content_type.partition(";")[0]
    assert downloaded.byte_length == len(content)
    assert (downloaded.width, downloaded.height) == (3, 2)
    assert len(downloaded.byte_sha256) == 64
    assert downloaded.redirect_urls == ()


@pytest.mark.skipif(not features.check("webp"), reason="Pillow was built without WebP")
def test_download_supports_static_webp() -> None:
    content = image_bytes("WEBP")
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, content=content, headers={"Content-Type": "image/webp"})
    )

    assert downloader(transport).download(IMAGE_URL).media_format == MediaFormat.WEBP


def test_download_decodes_only_the_first_gif_frame() -> None:
    content = image_bytes("GIF", frames=2)
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, content=content, headers={"Content-Type": "image/gif"})
    )

    result = downloader(transport).download(IMAGE_URL)

    assert result.media_format == MediaFormat.GIF
    assert (result.width, result.height) == (3, 2)


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://mmbiz.qpic.cn/a.png", MediaItemReason.UNSAFE_DESTINATION),
        ("https://example.com/a.png", MediaItemReason.BLOCKED_HOST),
        ("https://mmbiz.qpic.cn.evil.test/a.png", MediaItemReason.BLOCKED_HOST),
        ("https://user:secret@mmbiz.qpic.cn/a.png", MediaItemReason.UNSAFE_DESTINATION),
        ("https://mmbiz.qpic.cn:444/a.png", MediaItemReason.UNSAFE_DESTINATION),
    ],
)
def test_download_rejects_unapproved_destinations_before_http(
    url: str,
    reason: MediaItemReason,
) -> None:
    transport = httpx.MockTransport(lambda _: pytest.fail("unsafe URL must not reach HTTP"))

    with pytest.raises(MediaDownloadFailure) as raised:
        downloader(transport).download(url)

    assert raised.value.reason == reason


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.1", "169.254.1.1", "192.0.2.1", "::1", "fc00::1", "fe80::1"],
)
def test_download_rejects_any_non_public_resolved_address(address: str) -> None:
    resolver = StubResolver("8.8.8.8", address)
    transport = httpx.MockTransport(lambda _: pytest.fail("unsafe IP must not reach HTTP"))

    with pytest.raises(MediaDownloadFailure) as raised:
        downloader(transport, resolver=resolver).download(IMAGE_URL)

    assert raised.value.reason == MediaItemReason.UNSAFE_DESTINATION


def test_download_validates_and_records_every_redirect_without_forwarding_cookies() -> None:
    content = image_bytes()
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        assert request.headers["accept-encoding"] == "identity"
        if len(requests) == 1:
            return httpx.Response(
                302,
                headers={"Location": "/next/image.png#ignored", "Set-Cookie": "secret=value"},
            )
        return httpx.Response(200, content=content, headers={"Content-Type": "image/png"})

    resolver = StubResolver("8.8.8.8")
    result = downloader(httpx.MockTransport(respond), resolver=resolver).download(IMAGE_URL)

    assert len(requests) == 2
    assert resolver.calls == [("mmbiz.qpic.cn", 443), ("mmbiz.qpic.cn", 443)]
    assert result.final_url == "https://mmbiz.qpic.cn/next/image.png"
    assert result.redirect_urls == ("https://mmbiz.qpic.cn/next/image.png",)


def test_download_rejects_unapproved_redirect_before_second_http_request() -> None:
    requests = 0

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(302, headers={"Location": "https://evil.test/image.png"})

    with pytest.raises(MediaDownloadFailure) as raised:
        downloader(httpx.MockTransport(respond)).download(IMAGE_URL)

    assert raised.value.reason == MediaItemReason.BLOCKED_HOST
    assert requests == 1


def test_download_stops_after_five_redirects() -> None:
    requests = 0

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(302, headers={"Location": f"/redirect/{requests}"})

    with pytest.raises(MediaDownloadFailure, match="redirect limit") as raised:
        downloader(httpx.MockTransport(respond)).download(IMAGE_URL)

    assert raised.value.reason == MediaItemReason.DOWNLOAD_FAILED
    assert requests == 6


def test_download_retries_one_network_failure_and_revalidates_dns() -> None:
    content = image_bytes()
    requests = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, content=content, headers={"Content-Type": "image/png"})

    resolver = StubResolver("8.8.8.8")
    result = downloader(httpx.MockTransport(respond), resolver=resolver).download(IMAGE_URL)

    assert result.media_format == MediaFormat.PNG
    assert requests == 2
    assert len(resolver.calls) == 2


def test_download_retries_dns_failure_without_attempting_http() -> None:
    resolver = FailingResolver()
    transport = httpx.MockTransport(lambda _: pytest.fail("failed DNS must not reach HTTP"))

    with pytest.raises(MediaDownloadFailure) as raised:
        downloader(transport, resolver=resolver).download(IMAGE_URL)

    assert raised.value.reason == MediaItemReason.DOWNLOAD_FAILED
    assert resolver.calls == 2


def test_download_maps_exhausted_timeout_retry() -> None:
    requests = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(MediaDownloadFailure) as raised:
        downloader(httpx.MockTransport(respond)).download(IMAGE_URL)

    assert raised.value.reason == MediaItemReason.DOWNLOAD_TIMEOUT
    assert requests == 2


def test_download_bounds_retry_after_and_retries_429_once() -> None:
    content = image_bytes()
    requests = 0
    waits: list[float] = []

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(429, headers={"Retry-After": "1000"})
        return httpx.Response(200, content=content, headers={"Content-Type": "image/png"})

    result = downloader(httpx.MockTransport(respond), sleep=waits.append).download(IMAGE_URL)

    assert result.media_format == MediaFormat.PNG
    assert waits == [10.0]
    assert requests == 2


def test_download_maps_second_429_after_the_single_retry() -> None:
    requests = 0

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(429)

    with pytest.raises(MediaDownloadFailure, match="rate limited after one retry") as raised:
        downloader(httpx.MockTransport(respond), sleep=lambda _: None).download(IMAGE_URL)

    assert raised.value.reason == MediaItemReason.DOWNLOAD_FAILED
    assert requests == 2


def test_download_timeout_covers_retry_after_and_the_entire_image() -> None:
    clock = FakeClock()
    requests = 0

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(429, headers={"Retry-After": "10"})

    with pytest.raises(MediaDownloadFailure) as raised:
        downloader(
            httpx.MockTransport(respond),
            timeout_seconds=5,
            sleep=clock.sleep,
            clock=clock,
        ).download(IMAGE_URL)

    assert raised.value.reason == MediaItemReason.DOWNLOAD_TIMEOUT
    assert clock.now == 5
    assert requests == 1


def test_download_does_not_retry_forbidden_response() -> None:
    requests = 0

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(403)

    with pytest.raises(MediaDownloadFailure) as raised:
        downloader(httpx.MockTransport(respond)).download(IMAGE_URL)

    assert raised.value.reason == MediaItemReason.DOWNLOAD_FORBIDDEN
    assert requests == 1


def test_download_rejects_declared_content_length_before_reading() -> None:
    response = httpx.Response(
        200,
        headers={"Content-Length": "100"},
        stream=ChunkedBody(b"must not be decoded"),
    )

    with pytest.raises(MediaDownloadFailure) as raised:
        downloader(httpx.MockTransport(lambda _: response), max_bytes=10).download(IMAGE_URL)

    assert raised.value.reason == MediaItemReason.TOO_LARGE


def test_download_enforces_actual_bytes_when_content_length_is_misleading() -> None:
    response = httpx.Response(
        200,
        headers={"Content-Length": "1"},
        stream=ChunkedBody(b"12345", b"67890", b"overflow"),
    )

    with pytest.raises(MediaDownloadFailure) as raised:
        downloader(httpx.MockTransport(lambda _: response), max_bytes=10).download(IMAGE_URL)

    assert raised.value.reason == MediaItemReason.TOO_LARGE


def test_download_accepts_a_lower_per_call_byte_budget() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            stream=ChunkedBody(b"12345", b"6"),
            headers={"Content-Type": "image/png"},
        )
    )

    with pytest.raises(MediaDownloadFailure) as raised:
        downloader(transport).download(IMAGE_URL, max_bytes=5)

    assert raised.value.reason == MediaItemReason.TOO_LARGE


def test_cached_bytes_are_redecoded_without_dns_or_http() -> None:
    content = image_bytes("PNG")
    resolver = StubResolver("8.8.8.8")
    transport = httpx.MockTransport(lambda _: pytest.fail("cache validation must be offline"))

    result = downloader(transport, resolver=resolver).validate_cached(
        source_url=IMAGE_URL,
        final_url="https://mmbiz.qpic.cn/final/640",
        content=content,
        media_type="image/png",
        max_bytes=len(content),
    )

    assert result.content == content
    assert result.final_url == "https://mmbiz.qpic.cn/final/640"
    assert resolver.calls == []


def test_download_rejects_http_content_encoding_before_decoding() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            stream=ChunkedBody(image_bytes()),
            headers={"Content-Type": "image/png", "Content-Encoding": "gzip"},
        )
    )

    with pytest.raises(MediaDownloadFailure) as raised:
        downloader(transport).download(IMAGE_URL)

    assert raised.value.reason == MediaItemReason.UNSUPPORTED_FORMAT


@pytest.mark.parametrize(
    ("content", "content_type", "reason"),
    [
        (b"", "image/png", MediaItemReason.MALFORMED_IMAGE),
        (b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', "image/svg+xml", MediaItemReason.UNSUPPORTED_FORMAT),
        (image_bytes("PNG")[:-10], "image/png", MediaItemReason.MALFORMED_IMAGE),
        (image_bytes("PNG"), "image/jpeg", MediaItemReason.UNSUPPORTED_FORMAT),
    ],
)
def test_download_rejects_invalid_or_inconsistent_content(
    content: bytes,
    content_type: str,
    reason: MediaItemReason,
) -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, content=content, headers={"Content-Type": content_type})
    )

    with pytest.raises(MediaDownloadFailure) as raised:
        downloader(transport).download(IMAGE_URL)

    assert raised.value.reason == reason


def test_download_rejects_decoded_pixel_limit() -> None:
    content = image_bytes(size=(3, 2))
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, content=content, headers={"Content-Type": "image/png"})
    )

    with pytest.raises(MediaDownloadFailure) as raised:
        downloader(transport, max_pixels=5).download(IMAGE_URL)

    assert raised.value.reason == MediaItemReason.PIXEL_LIMIT


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("max_bytes", MAX_IMAGE_BYTES + 1),
        ("max_pixels", MAX_IMAGE_PIXELS + 1),
        ("timeout_seconds", 20.1),
        ("timeout_seconds", 0),
    ],
)
def test_download_limits_may_only_be_lowered(option: str, value: int | float) -> None:
    with pytest.raises(ValueError):
        MediaDownloader(**{option: value})
