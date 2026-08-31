"""Bounded, credential-free downloader for approved WeChat article images."""

from __future__ import annotations

import hashlib
import ipaddress
import socket
import time
import warnings
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from io import BytesIO
from typing import Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from PIL import Image, UnidentifiedImageError

from wxcli.media.models import (
    MAX_IMAGE_BYTES,
    MAX_IMAGE_PIXELS,
    MediaFormat,
    MediaItemReason,
)

APPROVED_MEDIA_HOSTS = frozenset({"mmbiz.qpic.cn"})
MAX_DOWNLOAD_SECONDS = 20.0
MAX_REDIRECTS = 5
MAX_RETRY_AFTER_SECONDS = 10.0
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_FORMAT_MEDIA_TYPES = {
    MediaFormat.JPEG: "image/jpeg",
    MediaFormat.PNG: "image/png",
    MediaFormat.WEBP: "image/webp",
    MediaFormat.GIF: "image/gif",
}
_PIL_FORMATS = {
    "JPEG": MediaFormat.JPEG,
    "PNG": MediaFormat.PNG,
    "WEBP": MediaFormat.WEBP,
    "GIF": MediaFormat.GIF,
}


class HostResolver(Protocol):
    """Resolve a host into every address a connection may use."""

    def resolve(self, host: str, port: int) -> tuple[str, ...]: ...


class SystemHostResolver:
    """Resolve through the operating system without changing connection state."""

    def resolve(self, host: str, port: int) -> tuple[str, ...]:
        addresses = {
            str(result[4][0])
            for result in socket.getaddrinfo(
                host,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
            )
        }
        return tuple(sorted(addresses))


@dataclass(frozen=True, slots=True)
class DownloadedMedia:
    """Validated original bytes and observations needed by later analyzers."""

    source_url: str
    final_url: str
    content: bytes = field(repr=False)
    byte_sha256: str
    media_format: MediaFormat
    media_type: str
    byte_length: int
    width: int
    height: int
    redirect_urls: tuple[str, ...]


@dataclass(slots=True)
class MediaDownloadFailure(Exception):
    """Expected per-image failure with a stable Media Evidence reason."""

    reason: MediaItemReason
    message: str

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class _Payload:
    url: str
    content_type: str | None
    content: bytes


@dataclass(frozen=True, slots=True)
class _Redirect:
    location: str


class MediaDownloader:
    """Download one approved image under fixed security and resource boundaries."""

    def __init__(
        self,
        *,
        resolver: HostResolver | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        max_bytes: int = MAX_IMAGE_BYTES,
        max_pixels: int = MAX_IMAGE_PIXELS,
        timeout_seconds: float = MAX_DOWNLOAD_SECONDS,
    ) -> None:
        if not 1 <= max_bytes <= MAX_IMAGE_BYTES:
            raise ValueError(f"max_bytes must be between 1 and {MAX_IMAGE_BYTES}.")
        if not 1 <= max_pixels <= MAX_IMAGE_PIXELS:
            raise ValueError(f"max_pixels must be between 1 and {MAX_IMAGE_PIXELS}.")
        if not 0 < timeout_seconds <= MAX_DOWNLOAD_SECONDS:
            raise ValueError(
                f"timeout_seconds must be greater than zero and at most {MAX_DOWNLOAD_SECONDS}."
            )
        self._resolver = resolver or SystemHostResolver()
        self._transport = transport
        self._sleep = sleep
        self._clock = clock
        self._max_bytes = max_bytes
        self._max_pixels = max_pixels
        self._timeout_seconds = timeout_seconds

    def download(self, source_url: str) -> DownloadedMedia:
        """Download and safely decode one image; never follow an unvalidated redirect."""
        current_url = self._normalize_destination(source_url)
        redirect_urls: list[str] = []
        deadline = self._clock() + self._timeout_seconds
        timeout = httpx.Timeout(self._timeout_seconds)
        headers = {
            "Accept": "image/webp,image/png,image/jpeg,image/gif;q=0.9",
            "User-Agent": "wechat-oa-media/0.6",
        }
        with httpx.Client(
            transport=self._transport,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            headers=headers,
        ) as client:
            while True:
                result = self._request_with_retry(client, current_url, deadline)
                if isinstance(result, _Payload):
                    return self._validate_payload(
                        source_url=source_url,
                        redirect_urls=tuple(redirect_urls),
                        payload=result,
                    )
                if len(redirect_urls) >= MAX_REDIRECTS:
                    raise MediaDownloadFailure(
                        MediaItemReason.DOWNLOAD_FAILED,
                        "Image download exceeded the redirect limit.",
                    )
                current_url = self._normalize_destination(urljoin(current_url, result.location))
                redirect_urls.append(current_url)

    def _request_with_retry(
        self,
        client: httpx.Client,
        url: str,
        deadline: float,
    ) -> _Payload | _Redirect:
        last_error: httpx.RequestError | None = None
        for attempt in range(2):
            try:
                remaining = self._remaining_seconds(deadline)
                self._validate_resolved_addresses(url)
                remaining = self._remaining_seconds(deadline)
                client.cookies.clear()
                with client.stream("GET", url, timeout=remaining) as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise MediaDownloadFailure(
                                MediaItemReason.DOWNLOAD_FAILED,
                                "Image redirect did not include a destination.",
                            )
                        return _Redirect(location)
                    if response.status_code == 429 and attempt == 0:
                        delay = min(
                            _retry_after_delay(response.headers.get("retry-after")),
                            self._remaining_seconds(deadline),
                        )
                        self._sleep(delay)
                        continue
                    if response.status_code in {401, 403}:
                        raise MediaDownloadFailure(
                            MediaItemReason.DOWNLOAD_FORBIDDEN,
                            "The approved image host refused the download.",
                        )
                    if response.status_code != 200:
                        raise MediaDownloadFailure(
                            MediaItemReason.DOWNLOAD_FAILED,
                            f"Image download returned HTTP {response.status_code}.",
                        )
                    content_encoding = response.headers.get("content-encoding")
                    if content_encoding is not None and content_encoding.casefold() != "identity":
                        raise MediaDownloadFailure(
                            MediaItemReason.UNSUPPORTED_FORMAT,
                            "Encoded HTTP image bodies are not accepted as original media bytes.",
                        )
                    content_length = _content_length(response.headers.get("content-length"))
                    if content_length is not None and content_length > self._max_bytes:
                        raise MediaDownloadFailure(
                            MediaItemReason.TOO_LARGE,
                            "Image exceeds the configured byte limit.",
                        )
                    content = self._read_bounded(response.iter_bytes())
                    return _Payload(
                        url=url,
                        content_type=response.headers.get("content-type"),
                        content=content,
                    )
            except httpx.RequestError as error:
                last_error = error
                if attempt == 0:
                    continue
        if isinstance(last_error, httpx.TimeoutException):
            raise MediaDownloadFailure(
                MediaItemReason.DOWNLOAD_TIMEOUT,
                "Image download timed out after one retry.",
            ) from last_error
        if last_error is not None:
            raise MediaDownloadFailure(
                MediaItemReason.DOWNLOAD_FAILED,
                "Image download failed after one retry.",
            ) from last_error
        raise MediaDownloadFailure(
            MediaItemReason.DOWNLOAD_FAILED,
            "Image download was rate limited after one retry.",
        )

    def _remaining_seconds(self, deadline: float) -> float:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise MediaDownloadFailure(
                MediaItemReason.DOWNLOAD_TIMEOUT,
                "Image download exceeded its total time limit.",
            )
        return remaining

    def _read_bounded(self, chunks: Iterable[bytes]) -> bytes:
        content = bytearray()
        for chunk in chunks:
            if len(content) + len(chunk) > self._max_bytes:
                raise MediaDownloadFailure(
                    MediaItemReason.TOO_LARGE,
                    "Image exceeds the configured byte limit.",
                )
            content.extend(chunk)
        if not content:
            raise MediaDownloadFailure(
                MediaItemReason.MALFORMED_IMAGE,
                "Image response was empty.",
            )
        return bytes(content)

    def _normalize_destination(self, url: str) -> str:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as error:
            raise MediaDownloadFailure(
                MediaItemReason.UNSAFE_DESTINATION,
                "Image URL has an invalid destination.",
            ) from error
        host = parsed.hostname
        if parsed.scheme.casefold() != "https" or host is None:
            raise MediaDownloadFailure(
                MediaItemReason.UNSAFE_DESTINATION,
                "Only HTTPS image destinations are allowed.",
            )
        normalized_host = host.casefold()
        if normalized_host not in APPROVED_MEDIA_HOSTS:
            raise MediaDownloadFailure(
                MediaItemReason.BLOCKED_HOST,
                "Image host is not an approved WeChat media CDN.",
            )
        if parsed.username is not None or parsed.password is not None or port not in {None, 443}:
            raise MediaDownloadFailure(
                MediaItemReason.UNSAFE_DESTINATION,
                "Image URL contains disallowed authority information.",
            )
        if parsed.query and len(parsed.query) > 4096:
            raise MediaDownloadFailure(
                MediaItemReason.UNSAFE_DESTINATION,
                "Image URL query is too long.",
            )
        return urlunsplit(("https", normalized_host, parsed.path or "/", parsed.query, ""))

    def _validate_resolved_addresses(self, url: str) -> None:
        host = urlsplit(url).hostname
        assert host is not None
        try:
            addresses = self._resolver.resolve(host, 443)
        except OSError as error:
            raise httpx.ConnectError("Approved image host could not be resolved.") from error
        if not addresses:
            raise MediaDownloadFailure(
                MediaItemReason.UNSAFE_DESTINATION,
                "Image destination did not resolve to a public address.",
            )
        try:
            resolved = tuple(ipaddress.ip_address(address) for address in addresses)
        except ValueError as error:
            raise MediaDownloadFailure(
                MediaItemReason.UNSAFE_DESTINATION,
                "Image destination resolved to an invalid address.",
            ) from error
        if any(not address.is_global for address in resolved):
            raise MediaDownloadFailure(
                MediaItemReason.UNSAFE_DESTINATION,
                "Image destination resolved to a non-public address.",
            )

    def _validate_payload(
        self,
        *,
        source_url: str,
        redirect_urls: tuple[str, ...],
        payload: _Payload,
    ) -> DownloadedMedia:
        media_format, width, height = self._decode_image(payload.content)
        media_type = _FORMAT_MEDIA_TYPES[media_format]
        declared_type = _declared_media_type(payload.content_type)
        if declared_type is not None and declared_type != media_type:
            raise MediaDownloadFailure(
                MediaItemReason.UNSUPPORTED_FORMAT,
                "Declared media type does not match the decoded raster image.",
            )
        return DownloadedMedia(
            source_url=source_url,
            final_url=payload.url,
            content=payload.content,
            byte_sha256=hashlib.sha256(payload.content).hexdigest(),
            media_format=media_format,
            media_type=media_type,
            byte_length=len(payload.content),
            width=width,
            height=height,
            redirect_urls=redirect_urls,
        )

    def _decode_image(self, content: bytes) -> tuple[MediaFormat, int, int]:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(content)) as image:
                    media_format = _PIL_FORMATS.get(image.format or "")
                    if media_format is None:
                        raise MediaDownloadFailure(
                            MediaItemReason.UNSUPPORTED_FORMAT,
                            "Downloaded content is not a supported raster image.",
                        )
                    width, height = image.size
                    if width <= 0 or height <= 0:
                        raise MediaDownloadFailure(
                            MediaItemReason.MALFORMED_IMAGE,
                            "Decoded image has invalid dimensions.",
                        )
                    if width * height > self._max_pixels:
                        raise MediaDownloadFailure(
                            MediaItemReason.PIXEL_LIMIT,
                            "Decoded image exceeds the configured pixel limit.",
                        )
                    if media_format == MediaFormat.WEBP and getattr(image, "is_animated", False):
                        raise MediaDownloadFailure(
                            MediaItemReason.UNSUPPORTED_FORMAT,
                            "Animated WebP images are not supported.",
                        )
                    if media_format == MediaFormat.GIF:
                        image.seek(0)
                        image.load()
                    else:
                        image.verify()
                if media_format != MediaFormat.GIF:
                    with Image.open(BytesIO(content)) as decoded:
                        decoded.load()
        except MediaDownloadFailure:
            raise
        except Image.DecompressionBombWarning as error:
            raise MediaDownloadFailure(
                MediaItemReason.PIXEL_LIMIT,
                "Decoded image exceeds the safe pixel limit.",
            ) from error
        except Image.DecompressionBombError as error:
            raise MediaDownloadFailure(
                MediaItemReason.PIXEL_LIMIT,
                "Decoded image exceeds the safe pixel limit.",
            ) from error
        except UnidentifiedImageError as error:
            raise MediaDownloadFailure(
                MediaItemReason.UNSUPPORTED_FORMAT,
                "Downloaded content is not a supported raster image.",
            ) from error
        except (EOFError, OSError, SyntaxError, ValueError) as error:
            raise MediaDownloadFailure(
                MediaItemReason.MALFORMED_IMAGE,
                "Downloaded raster image could not be safely decoded.",
            ) from error
        return media_format, width, height


def _content_length(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _declared_media_type(value: str | None) -> str | None:
    if value is None:
        return None
    return value.partition(";")[0].strip().casefold() or None


def _retry_after_delay(value: str | None) -> float:
    if value is None:
        return 0.0
    try:
        return min(max(float(value), 0.0), MAX_RETRY_AFTER_SECONDS)
    except ValueError:
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return 0.0
        return min(max(delay, 0.0), MAX_RETRY_AFTER_SECONDS)
