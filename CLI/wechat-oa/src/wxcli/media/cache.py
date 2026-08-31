"""Dedicated, integrity-checked cache for public WeChat media bytes."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from wxcli.media.downloader import DownloadedMedia
from wxcli.media.models import MAX_IMAGE_BYTES

MEDIA_CACHE_SCHEMA_VERSION = "1"
MEDIA_CACHE_TTL = timedelta(days=7)
MAX_MEDIA_CACHE_BYTES = 1024 * 1024 * 1024
_SHA256_LENGTH = 64


@dataclass(frozen=True, slots=True)
class CachedMedia:
    """Untrusted cached bytes that must be decoded again before analysis."""

    source_url: str
    final_url: str
    content: bytes
    byte_sha256: str
    media_type: str


@dataclass(frozen=True, slots=True)
class MediaCacheCleanup:
    """Deterministic cleanup observations for diagnostics and tests."""

    expired_references: int = 0
    invalid_references: int = 0
    orphaned_blobs: int = 0
    evicted_references: int = 0
    evicted_blobs: int = 0


@dataclass(frozen=True, slots=True)
class _Reference:
    path: Path
    source_url: str
    final_url: str
    byte_sha256: str
    media_type: str
    expires_at: datetime
    last_accessed_at: datetime


class MediaCache:
    """Seven-day SHA-256 blob cache with per-URL references and LRU eviction."""

    def __init__(
        self,
        directory: Path,
        *,
        ttl: timedelta = MEDIA_CACHE_TTL,
        max_size_bytes: int = MAX_MEDIA_CACHE_BYTES,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl <= timedelta(0) or ttl > MEDIA_CACHE_TTL:
            raise ValueError("Media Cache ttl must be positive and at most seven days.")
        if not 1 <= max_size_bytes <= MAX_MEDIA_CACHE_BYTES:
            raise ValueError(
                f"Media Cache size must be between 1 and {MAX_MEDIA_CACHE_BYTES} bytes."
            )
        self.directory = directory
        self.ttl = ttl
        self.max_size_bytes = max_size_bytes
        self._now = now or (lambda: datetime.now(UTC))
        self._references = directory / "references"
        self._blobs = directory / "blobs"

    def get(self, source_url: str) -> CachedMedia | None:
        """Return hash-verified bytes and refresh LRU access, or remove one invalid entry."""
        path = self._reference_path(source_url)
        reference = self._read_reference(path)
        if reference is None or reference.source_url != source_url:
            path.unlink(missing_ok=True)
            return None
        now = self._aware_now()
        if reference.expires_at <= now:
            path.unlink(missing_ok=True)
            self._remove_blob_if_unreferenced(reference.byte_sha256)
            return None
        blob = self._blob_path(reference.byte_sha256)
        if _is_link_like(blob):
            blob.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            return None
        try:
            content = blob.read_bytes()
        except OSError:
            path.unlink(missing_ok=True)
            return None
        if (
            not content
            or len(content) > MAX_IMAGE_BYTES
            or hashlib.sha256(content).hexdigest() != reference.byte_sha256
        ):
            blob.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            return None
        refreshed = _Reference(
            path=path,
            source_url=reference.source_url,
            final_url=reference.final_url,
            byte_sha256=reference.byte_sha256,
            media_type=reference.media_type,
            expires_at=reference.expires_at,
            last_accessed_at=now,
        )
        self._write_reference(refreshed)
        return CachedMedia(
            source_url=reference.source_url,
            final_url=reference.final_url,
            content=content,
            byte_sha256=reference.byte_sha256,
            media_type=reference.media_type,
        )

    def put(self, media: DownloadedMedia) -> bool:
        """Atomically store validated bytes; return false when the configured cap cannot hold them."""
        actual_hash = hashlib.sha256(media.content).hexdigest()
        if actual_hash != media.byte_sha256:
            raise ValueError("Downloaded media SHA-256 does not match its bytes.")
        if len(media.content) > self.max_size_bytes:
            return False
        self._ensure_safe_directories()
        blob = self._blob_path(actual_hash)
        if _is_link_like(blob):
            blob.unlink(missing_ok=True)
        try:
            existing_hash = hashlib.sha256(blob.read_bytes()).hexdigest()
        except OSError:
            existing_hash = None
        if existing_hash != actual_hash:
            _atomic_write(blob, media.content)
        now = self._aware_now()
        reference = _Reference(
            path=self._reference_path(media.source_url),
            source_url=media.source_url,
            final_url=media.final_url,
            byte_sha256=actual_hash,
            media_type=media.media_type,
            expires_at=now + self.ttl,
            last_accessed_at=now,
        )
        self._write_reference(reference)
        self.cleanup()
        return reference.path.exists() and blob.exists()

    def cleanup(self) -> MediaCacheCleanup:
        """Remove expired/corrupt entries, orphan blobs, then oldest references until bounded."""
        if not self.directory.exists():
            return MediaCacheCleanup()
        self._ensure_safe_directories()
        now = self._aware_now()
        expired = 0
        invalid = 0
        references: list[_Reference] = []
        for path in sorted(self._references.glob("*.json"), key=lambda item: item.name):
            reference = self._read_reference(path)
            if reference is None:
                path.unlink(missing_ok=True)
                invalid += 1
            elif reference.expires_at <= now:
                path.unlink(missing_ok=True)
                expired += 1
            else:
                references.append(reference)

        referenced_hashes = {reference.byte_sha256 for reference in references}
        orphaned = 0
        for blob in sorted(self._blobs.glob("*.bin"), key=lambda item: item.name):
            if blob.stem not in referenced_hashes or not _is_sha256(blob.stem) or _is_link_like(blob):
                blob.unlink(missing_ok=True)
                orphaned += 1

        available_references: list[_Reference] = []
        for reference in references:
            if self._blob_path(reference.byte_sha256).is_file():
                available_references.append(reference)
            else:
                reference.path.unlink(missing_ok=True)
                invalid += 1
        references = available_references
        evicted_references = 0
        evicted_blobs = 0
        for reference in sorted(
            references,
            key=lambda item: (item.last_accessed_at, item.path.name),
        ):
            if self._current_size() <= self.max_size_bytes:
                break
            reference.path.unlink(missing_ok=True)
            evicted_references += 1
            if self._remove_blob_if_unreferenced(reference.byte_sha256):
                evicted_blobs += 1

        return MediaCacheCleanup(
            expired_references=expired,
            invalid_references=invalid,
            orphaned_blobs=orphaned,
            evicted_references=evicted_references,
            evicted_blobs=evicted_blobs,
        )

    def clear(self) -> int:
        """Remove only Media Cache records and temporary files, never other wxcli state."""
        if not self.directory.exists():
            return 0
        self._ensure_safe_directories()
        removed = 0
        for directory, patterns in (
            (self._references, ("*.json", ".tmp-*")),
            (self._blobs, ("*.bin", ".tmp-*")),
        ):
            for pattern in patterns:
                for path in directory.glob(pattern):
                    path.unlink(missing_ok=True)
                    removed += 1
        return removed

    def _read_reference(self, path: Path) -> _Reference | None:
        if _is_link_like(path):
            return None
        try:
            payload: Any = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("schema_version") != MEDIA_CACHE_SCHEMA_VERSION:
                return None
            source_url = payload["source_url"]
            final_url = payload["final_url"]
            byte_sha256 = payload["byte_sha256"]
            media_type = payload["media_type"]
            if not all(isinstance(value, str) for value in (source_url, final_url, byte_sha256, media_type)):
                return None
            if not _is_sha256(byte_sha256):
                return None
            expires_at = datetime.fromisoformat(payload["expires_at"])
            last_accessed_at = datetime.fromisoformat(payload["last_accessed_at"])
            if not _is_aware(expires_at) or not _is_aware(last_accessed_at):
                return None
            return _Reference(
                path=path,
                source_url=source_url,
                final_url=final_url,
                byte_sha256=byte_sha256,
                media_type=media_type,
                expires_at=expires_at.astimezone(UTC),
                last_accessed_at=last_accessed_at.astimezone(UTC),
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _write_reference(self, reference: _Reference) -> None:
        self._ensure_safe_directories()
        payload = {
            "schema_version": MEDIA_CACHE_SCHEMA_VERSION,
            "source_url": reference.source_url,
            "final_url": reference.final_url,
            "byte_sha256": reference.byte_sha256,
            "media_type": reference.media_type,
            "expires_at": reference.expires_at.isoformat(),
            "last_accessed_at": reference.last_accessed_at.isoformat(),
        }
        _atomic_write(
            reference.path,
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            ),
        )

    def _ensure_safe_directories(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        for directory in (self.directory, self._references, self._blobs):
            if directory.exists() and (_is_link_like(directory) or not directory.is_dir()):
                raise ValueError("Media Cache directories cannot be links, junctions, or files.")
            directory.mkdir(exist_ok=True)

    def _remove_blob_if_unreferenced(self, byte_sha256: str) -> bool:
        for path in self._references.glob("*.json"):
            reference = self._read_reference(path)
            if reference is not None and reference.byte_sha256 == byte_sha256:
                return False
        blob = self._blob_path(byte_sha256)
        existed = blob.exists() or _is_link_like(blob)
        blob.unlink(missing_ok=True)
        return existed

    def _current_size(self) -> int:
        paths = tuple(self._references.glob("*.json")) + tuple(self._blobs.glob("*.bin"))
        size = 0
        for path in paths:
            try:
                size += path.lstat().st_size
            except OSError:
                continue
        return size

    def _reference_path(self, source_url: str) -> Path:
        digest = hashlib.sha256(source_url.encode("utf-8")).hexdigest()
        return self._references / f"{digest}.json"

    def _blob_path(self, byte_sha256: str) -> Path:
        return self._blobs / f"{byte_sha256}.bin"

    def _aware_now(self) -> datetime:
        value = self._now()
        if not _is_aware(value):
            raise ValueError("Media Cache clock must return a timezone-aware datetime.")
        return value.astimezone(UTC)


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.parent / f".tmp-{uuid4().hex}"
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _is_sha256(value: str) -> bool:
    return len(value) == _SHA256_LENGTH and all(character in "0123456789abcdef" for character in value)


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _is_link_like(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()
