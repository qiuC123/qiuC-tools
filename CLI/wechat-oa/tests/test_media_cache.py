from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wxcli.media import DownloadedMedia, MediaCache, MediaFormat
from wxcli.media.cache import MAX_MEDIA_CACHE_BYTES, MEDIA_CACHE_TTL


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


def media(url: str, content: bytes) -> DownloadedMedia:
    return DownloadedMedia(
        source_url=url,
        final_url=url,
        content=content,
        byte_sha256=hashlib.sha256(content).hexdigest(),
        media_format=MediaFormat.PNG,
        media_type="image/png",
        byte_length=len(content),
        width=2,
        height=2,
        redirect_urls=(),
    )


def test_cache_round_trip_rehashes_bytes_and_deduplicates_blobs(tmp_path: Path) -> None:
    cache = MediaCache(tmp_path / "media")
    content = b"same public image bytes"
    first_url = "https://mmbiz.qpic.cn/first/640"
    second_url = "https://mmbiz.qpic.cn/second/640"

    assert cache.put(media(first_url, content)) is True
    assert cache.put(media(second_url, content)) is True

    assert cache.get(first_url) is not None
    assert cache.get(second_url) is not None
    assert len(list((tmp_path / "media" / "blobs").glob("*.bin"))) == 1
    assert len(list((tmp_path / "media" / "references").glob("*.json"))) == 2
    assert not list((tmp_path / "media").rglob(".tmp-*"))


def test_cache_removes_only_corrupt_blob_and_reference(tmp_path: Path) -> None:
    cache = MediaCache(tmp_path / "media")
    url = "https://mmbiz.qpic.cn/corrupt/640"
    item = media(url, b"valid bytes")
    cache.put(item)
    blob = tmp_path / "media" / "blobs" / f"{item.byte_sha256}.bin"
    blob.write_bytes(b"tampered bytes")

    assert cache.get(url) is None
    assert not blob.exists()
    assert not list((tmp_path / "media" / "references").glob("*.json"))


def test_cache_expiry_removes_reference_and_unshared_blob(tmp_path: Path) -> None:
    clock = Clock()
    cache = MediaCache(tmp_path / "media", now=clock)
    url = "https://mmbiz.qpic.cn/expires/640"
    cache.put(media(url, b"public bytes"))

    clock.advance(MEDIA_CACHE_TTL)

    assert cache.get(url) is None
    assert not list((tmp_path / "media" / "blobs").glob("*.bin"))


def test_cleanup_removes_corrupt_references_and_orphan_blobs(tmp_path: Path) -> None:
    cache = MediaCache(tmp_path / "media")
    valid = media("https://mmbiz.qpic.cn/valid/640", b"valid")
    cache.put(valid)
    references = tmp_path / "media" / "references"
    blobs = tmp_path / "media" / "blobs"
    (references / f"{'f' * 64}.json").write_text("not-json", encoding="utf-8")
    (blobs / f"{'e' * 64}.bin").write_bytes(b"orphan")

    result = cache.cleanup()

    assert result.invalid_references == 1
    assert result.orphaned_blobs == 1
    assert cache.get(valid.source_url) is not None


def test_cleanup_evicts_least_recently_used_reference_first(tmp_path: Path) -> None:
    clock = Clock()
    cache = MediaCache(tmp_path / "media", max_size_bytes=3300, now=clock)
    first = media("https://mmbiz.qpic.cn/first/640", b"a" * 1024)
    second = media("https://mmbiz.qpic.cn/second/640", b"b" * 1024)
    third = media("https://mmbiz.qpic.cn/third/640", b"c" * 1024)
    cache.put(first)
    clock.advance(timedelta(seconds=1))
    cache.put(second)
    clock.advance(timedelta(seconds=1))
    assert cache.get(first.source_url) is not None
    clock.advance(timedelta(seconds=1))

    cache.put(third)

    assert cache.get(first.source_url) is not None
    assert cache.get(second.source_url) is None
    assert cache.get(third.source_url) is not None


def test_cache_does_not_store_item_larger_than_its_total_cap(tmp_path: Path) -> None:
    cache = MediaCache(tmp_path / "media", max_size_bytes=10)

    assert cache.put(media("https://mmbiz.qpic.cn/large/640", b"x" * 11)) is False
    assert cache.get("https://mmbiz.qpic.cn/large/640") is None


def test_cache_rejects_mismatched_input_hash(tmp_path: Path) -> None:
    cache = MediaCache(tmp_path / "media")
    item = media("https://mmbiz.qpic.cn/hash/640", b"valid")

    with pytest.raises(ValueError, match="SHA-256"):
        cache.put(replace(item, byte_sha256="f" * 64))


def test_cache_clear_removes_only_cache_records(tmp_path: Path) -> None:
    root = tmp_path / "media"
    cache = MediaCache(root)
    cache.put(media("https://mmbiz.qpic.cn/clear/640", b"valid"))
    unrelated = root / "keep.txt"
    unrelated.write_text("not a cache record", encoding="utf-8")

    removed = cache.clear()

    assert removed == 2
    assert unrelated.read_text(encoding="utf-8") == "not a cache record"
    assert cache.clear() == 0


def test_cache_discard_preserves_a_blob_shared_by_another_url(tmp_path: Path) -> None:
    cache = MediaCache(tmp_path / "media")
    content = b"shared"
    first = media("https://mmbiz.qpic.cn/first/640", content)
    second = media("https://mmbiz.qpic.cn/second/640", content)
    cache.put(first)
    cache.put(second)

    assert cache.discard(first.source_url) is True
    assert cache.get(first.source_url) is None
    assert cache.get(second.source_url) is not None
    assert cache.discard(first.source_url) is False


@pytest.mark.parametrize(
    "options",
    [
        {"ttl": timedelta(0)},
        {"ttl": MEDIA_CACHE_TTL + timedelta(seconds=1)},
        {"max_size_bytes": 0},
        {"max_size_bytes": MAX_MEDIA_CACHE_BYTES + 1},
    ],
)
def test_cache_limits_may_only_be_lowered(options: dict[str, object], tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        MediaCache(tmp_path / "media", **options)  # type: ignore[arg-type]


def test_cache_ignores_reference_pointing_to_another_source_url(tmp_path: Path) -> None:
    cache = MediaCache(tmp_path / "media")
    requested_url = "https://mmbiz.qpic.cn/requested/640"
    cache.put(media(requested_url, b"valid"))
    reference = next((tmp_path / "media" / "references").glob("*.json"))
    payload = json.loads(reference.read_text(encoding="utf-8"))
    payload["source_url"] = "https://mmbiz.qpic.cn/substituted/640"
    reference.write_text(json.dumps(payload), encoding="utf-8")

    assert cache.get(requested_url) is None
