from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wxcli.evidence import AccountIdentityEvidence, ArticleEvidence, IdentityStatus
from wxcli.errors import ValidationError
from wxcli.media.bundle import EvidenceBundleWriter, preflight_bundle_destination
from wxcli.media.downloader import DownloadedMedia
from wxcli.media.models import (
    MediaFormat,
    MediaItemEvidence,
    MediaItemStatus,
    OCREvidence,
    OCRStatus,
    QREvidence,
    QRStatus,
    build_media_evidence,
)
from wxcli.media.orchestration import (
    ArticleMediaDownloads,
    MediaAcquisitionItem,
    MediaAcquisitionStatus,
)
from wxcli.models import Article, Provider


def _bundle_inputs() -> tuple[ArticleEvidence, object, ArticleMediaDownloads, bytes]:
    content = b"validated-original-image-bytes"
    byte_sha256 = hashlib.sha256(content).hexdigest()
    source_url = "https://mmbiz.qpic.cn/example/640"
    article = Article(
        title="Campus hiring",
        content_markdown="# Campus hiring\n\nApply now.",
        source_url="https://mp.weixin.qq.com/s/TOKEN",
        images=[source_url],
        provider=Provider.HTTP,
    )
    article_evidence = ArticleEvidence(
        article=article,
        account_identity=AccountIdentityEvidence(status=IdentityStatus.UNKNOWN),
        last_verified_at=datetime(2026, 1, 1, tzinfo=UTC),
        content_sha256="a" * 64,
        evidence_sha256="b" * 64,
    )
    media = DownloadedMedia(
        source_url=source_url,
        final_url=source_url,
        content=content,
        byte_sha256=byte_sha256,
        media_format=MediaFormat.PNG,
        media_type="image/png",
        byte_length=len(content),
        width=10,
        height=10,
        redirect_urls=(),
    )
    downloads = ArticleMediaDownloads(
        items=(
            MediaAcquisitionItem(
                index=0,
                source_url=source_url,
                status=MediaAcquisitionStatus.DOWNLOADED,
                media=media,
            ),
        ),
        total_bytes=len(content),
    )
    media_evidence = build_media_evidence(
        source_content_sha256=article_evidence.content_sha256,
        items=(
            MediaItemEvidence(
                index=0,
                source_url=source_url,
                status=MediaItemStatus.ANALYZED,
                byte_sha256=byte_sha256,
                media_format=MediaFormat.PNG,
                media_type="image/png",
                byte_length=len(content),
                width=10,
                height=10,
                qr=QREvidence(
                    source_byte_sha256=byte_sha256,
                    analyzer="test-qr",
                    analyzer_version="1",
                    status=QRStatus.NOT_FOUND,
                ),
                ocr=OCREvidence(
                    source_byte_sha256=byte_sha256,
                    analyzer="test-ocr",
                    analyzer_version="1",
                    status=OCRStatus.UNAVAILABLE,
                ),
            ),
        ),
        analysis_started_at=datetime(2026, 1, 1, tzinfo=UTC),
        analysis_finished_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
    )
    return article_evidence, media_evidence, downloads, content


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_bundle_writes_a_verified_atomic_directory_with_original_bytes(
    tmp_path: Path,
) -> None:
    article, media_evidence, downloads, original = _bundle_inputs()
    destination = tmp_path / "evidence"
    writer = EvidenceBundleWriter(preflight_bundle_destination(destination))

    result = writer.create(
        article_evidence=article,
        media_evidence=media_evidence,
        downloads=downloads,
    )

    assert Path(result.path) == destination.resolve()
    assert result.metadata_only is False
    assert result.image_artifacts == 1
    image_path = next((destination / "images").iterdir())
    assert image_path.name == f"0000-{hashlib.sha256(original).hexdigest()}.png"
    assert image_path.read_bytes() == original
    assert (destination / "article.md").read_text(encoding="utf-8") == article.article.content_markdown

    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "1"
    assert manifest["source_content_sha256"] == article.content_sha256
    assert manifest["metadata_only"] is False
    assert result.manifest_sha256 == _sha256(destination / "manifest.json")
    for item in manifest["files"]:
        path = destination / item["path"]
        assert path.is_file()
        assert item["byte_length"] == path.stat().st_size
        assert item["sha256"] == _sha256(path)
    assert not list(tmp_path.glob(".evidence.staging-*"))


def test_metadata_only_keeps_manifests_without_writing_image_bytes(tmp_path: Path) -> None:
    article, media_evidence, downloads, _ = _bundle_inputs()
    destination = tmp_path / "metadata"

    result = EvidenceBundleWriter(preflight_bundle_destination(destination)).create(
        article_evidence=article,
        media_evidence=media_evidence,
        downloads=downloads,
        metadata_only=True,
    )

    assert result.metadata_only is True
    assert result.image_artifacts == 0
    assert not (destination / "images").exists()
    image_manifest = json.loads((destination / "images.json").read_text(encoding="utf-8"))
    assert image_manifest["items"][0]["artifact_path"] is None
    assert image_manifest["items"][0]["byte_sha256"] == hashlib.sha256(
        b"validated-original-image-bytes"
    ).hexdigest()


def test_preflight_rejects_existing_destination_without_changing_it(tmp_path: Path) -> None:
    destination = tmp_path / "existing"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(ValidationError, match="must not already exist"):
        preflight_bundle_destination(destination)

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_preflight_rejects_reparse_parent_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parent = tmp_path / "reparse-parent"
    parent.mkdir()
    destination = parent / "bundle"
    monkeypatch.setattr(
        "wxcli.media.bundle._is_reparse_point",
        lambda path: path == parent,
    )

    with pytest.raises(ValidationError, match="reparse"):
        preflight_bundle_destination(destination)

    assert not destination.exists()


def test_bundle_rejects_mismatched_original_bytes_and_cleans_staging(tmp_path: Path) -> None:
    article, media_evidence, downloads, _ = _bundle_inputs()
    assert downloads.items[0].media is not None
    tampered = replace(downloads.items[0].media, content=b"tampered")
    downloads = ArticleMediaDownloads(
        items=(replace(downloads.items[0], media=tampered),),
        total_bytes=tampered.byte_length,
    )
    destination = tmp_path / "mismatch"

    with pytest.raises(ValidationError, match="hash"):
        EvidenceBundleWriter(preflight_bundle_destination(destination)).create(
            article_evidence=article,
            media_evidence=media_evidence,
            downloads=downloads,
        )

    assert not destination.exists()
    assert not list(tmp_path.glob(".mismatch.staging-*"))


def test_bundle_interruption_removes_only_its_guarded_staging(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    article, media_evidence, downloads, _ = _bundle_inputs()
    destination = tmp_path / "interrupted"
    sentinel = tmp_path / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    def interrupt(path: Path, content: bytes) -> None:
        del path, content
        raise KeyboardInterrupt

    monkeypatch.setattr("wxcli.media.bundle._write_new_file", interrupt)

    with pytest.raises(KeyboardInterrupt):
        EvidenceBundleWriter(preflight_bundle_destination(destination)).create(
            article_evidence=article,
            media_evidence=media_evidence,
            downloads=downloads,
        )

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not destination.exists()
    assert not list(tmp_path.glob(".interrupted.staging-*"))
