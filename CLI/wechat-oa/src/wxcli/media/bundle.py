"""Guarded atomic Evidence Bundle creation for one analyzed Article."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from wxcli.evidence import ArticleEvidence
from wxcli.errors import ErrorCode, ValidationError, WxcliError
from wxcli.media.models import MediaEvidence, MediaItemStatus
from wxcli.media.orchestration import (
    ArticleMediaDownloads,
    MediaAcquisitionStatus,
)

BUNDLE_SCHEMA_VERSION: Literal["1"] = "1"
_STAGING_MARKER = ".staging-"


class BundleFile(BaseModel):
    """One hashed file listed by an Evidence Bundle manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=512)
    byte_length: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class EvidenceBundleManifest(BaseModel):
    """Versioned integrity manifest for one completed Evidence Bundle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = BUNDLE_SCHEMA_VERSION
    source_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    article_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata_only: bool
    files: tuple[BundleFile, ...]

    @model_validator(mode="after")
    def validate_files(self) -> EvidenceBundleManifest:
        paths = [item.path for item in self.files]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("Bundle manifest paths must be unique and sorted.")
        for value in paths:
            path = PurePosixPath(value)
            if path.is_absolute() or ".." in path.parts or "\\" in value:
                raise ValueError("Bundle manifest paths must be safe relative paths.")
        return self


class EvidenceBundleResult(BaseModel):
    """Safe summary returned after a Bundle has been atomically completed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = BUNDLE_SCHEMA_VERSION
    path: str = Field(min_length=1, max_length=32767)
    metadata_only: bool
    file_count: int = Field(ge=0)
    image_artifacts: int = Field(ge=0)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class MediaBundleResult(BaseModel):
    """Schema-v2 media result extended only for an explicitly requested Bundle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["2"] = "2"
    article_evidence: ArticleEvidence
    media_evidence: MediaEvidence
    bundle: EvidenceBundleResult

    @model_validator(mode="after")
    def validate_link(self) -> MediaBundleResult:
        if self.media_evidence.source_content_sha256 != self.article_evidence.content_sha256:
            raise ValueError("Media Evidence must link to the embedded Article Evidence.")
        return self


@dataclass(frozen=True, slots=True)
class BundleDestination:
    """A non-existing target under one existing, non-reparse parent directory."""

    path: Path
    parent: Path


def preflight_bundle_destination(destination: Path) -> BundleDestination:
    """Validate a local Bundle target without creating any file or directory."""
    expanded = destination.expanduser()
    absolute = Path(os.path.abspath(expanded))
    if os.path.lexists(absolute):
        raise ValidationError(
            "The Evidence Bundle destination must not already exist.",
            path=str(absolute),
        )
    parent = absolute.parent
    if not parent.exists() or not parent.is_dir():
        raise ValidationError(
            "The Evidence Bundle parent directory must already exist.",
            path=str(parent),
        )
    _guard_path_components(parent)
    resolved_parent = parent.resolve(strict=True)
    _guard_path_components(resolved_parent)
    resolved = resolved_parent / absolute.name
    if os.path.lexists(resolved):
        raise ValidationError(
            "The Evidence Bundle destination must not already exist.",
            path=str(resolved),
        )
    return BundleDestination(path=resolved, parent=resolved_parent)


class EvidenceBundleWriter:
    """Write, verify, and atomically expose one Evidence Bundle directory."""

    def __init__(self, destination: BundleDestination) -> None:
        self._destination = destination

    def create(
        self,
        *,
        article_evidence: ArticleEvidence,
        media_evidence: MediaEvidence,
        downloads: ArticleMediaDownloads,
        metadata_only: bool = False,
    ) -> EvidenceBundleResult:
        originals = _validate_inputs(article_evidence, media_evidence, downloads)
        _revalidate_destination(self._destination)
        prefix = f".{self._destination.path.name}{_STAGING_MARKER}"
        staging = self._destination.parent / f"{prefix}{uuid4().hex}"
        try:
            staging.mkdir()
            if _is_reparse_point(staging) or not staging.is_dir():
                raise ValidationError("The Evidence Bundle staging directory is unsafe.")
            manifest, image_artifacts = _write_bundle(
                staging,
                article_evidence=article_evidence,
                media_evidence=media_evidence,
                originals=originals,
                metadata_only=metadata_only,
            )
            manifest_path = staging / "manifest.json"
            manifest_bytes = _json_bytes(manifest)
            _write_new_file(manifest_path, manifest_bytes)
            _verify_bundle(staging, manifest)
            _revalidate_destination(self._destination)
            staging.rename(self._destination.path)
            return EvidenceBundleResult(
                path=str(self._destination.path),
                metadata_only=metadata_only,
                file_count=len(manifest.files) + 1,
                image_artifacts=image_artifacts,
                manifest_sha256=_bytes_sha256(manifest_bytes),
            )
        except KeyboardInterrupt:
            _cleanup_staging(staging, self._destination, prefix)
            raise
        except ValidationError:
            _cleanup_staging(staging, self._destination, prefix)
            raise
        except (OSError, ValueError, json.JSONDecodeError) as error:
            _cleanup_staging(staging, self._destination, prefix)
            raise WxcliError(
                ErrorCode.LOCAL_CONFIGURATION_ERROR,
                "Could not create the requested Evidence Bundle.",
                {"path": str(self._destination.path)},
            ) from error

    @property
    def destination(self) -> BundleDestination:
        return self._destination


def _validate_inputs(
    article_evidence: ArticleEvidence,
    media_evidence: MediaEvidence,
    downloads: ArticleMediaDownloads,
) -> dict[int, bytes]:
    if media_evidence.source_content_sha256 != article_evidence.content_sha256:
        raise ValidationError("Media Evidence is linked to different Article Evidence.")
    if len(downloads.items) != len(media_evidence.items):
        raise ValidationError("Media Evidence does not match the acquired image occurrences.")
    originals: dict[int, bytes] = {}
    for item, acquisition in zip(media_evidence.items, downloads.items, strict=True):
        if item.index != acquisition.index or item.source_url != acquisition.source_url:
            raise ValidationError("Media Evidence image ordering does not match acquisition.")
        if item.status != MediaItemStatus.ANALYZED:
            continue
        if acquisition.status != MediaAcquisitionStatus.DOWNLOADED or acquisition.media is None:
            raise ValidationError("Analyzed Media Evidence is missing its original image bytes.")
        original = acquisition.media
        actual_hash = _bytes_sha256(original.content)
        if actual_hash != original.byte_sha256 or actual_hash != item.byte_sha256:
            raise ValidationError("Original image byte hash does not match Media Evidence.")
        if (
            len(original.content) != original.byte_length
            or original.byte_length != item.byte_length
            or original.media_format != item.media_format
            or original.media_type != item.media_type
            or original.width != item.width
            or original.height != item.height
        ):
            raise ValidationError("Original image metadata does not match Media Evidence.")
        originals[item.index] = original.content
    return originals


def _write_bundle(
    staging: Path,
    *,
    article_evidence: ArticleEvidence,
    media_evidence: MediaEvidence,
    originals: dict[int, bytes],
    metadata_only: bool,
) -> tuple[EvidenceBundleManifest, int]:
    files: list[BundleFile] = []
    image_manifest_items: list[dict[str, object]] = []
    image_artifacts = 0

    for item in media_evidence.items:
        artifact_path: str | None = None
        if item.index in originals and not metadata_only:
            assert item.byte_sha256 is not None
            assert item.media_format is not None
            artifact_path = f"images/{item.index:04d}-{item.byte_sha256}.{item.media_format.value}"
            image_path = staging.joinpath(*PurePosixPath(artifact_path).parts)
            image_path.parent.mkdir(exist_ok=True)
            _write_new_file(image_path, originals[item.index])
            files.append(_bundle_file(staging, image_path))
            image_artifacts += 1
        image_manifest_items.append(
            item.model_dump(mode="json")
            | {
                "artifact_path": artifact_path,
            }
        )

    metadata: tuple[tuple[str, bytes], ...] = (
        ("article-evidence.v1.json", _json_bytes(article_evidence)),
        ("media-evidence.v1.json", _json_bytes(media_evidence)),
        ("article.md", article_evidence.article.content_markdown.encode("utf-8")),
        (
            "external-links.json",
            _json_bytes(
                {
                    "schema_version": "1",
                    "source_content_sha256": article_evidence.content_sha256,
                    "items": [item.model_dump(mode="json") for item in article_evidence.external_links],
                }
            ),
        ),
        (
            "images.json",
            _json_bytes(
                {
                    "schema_version": "1",
                    "source_content_sha256": article_evidence.content_sha256,
                    "metadata_only": metadata_only,
                    "items": image_manifest_items,
                }
            ),
        ),
    )
    for relative, content in metadata:
        path = staging / relative
        _write_new_file(path, content)
        files.append(_bundle_file(staging, path))

    return (
        EvidenceBundleManifest(
            source_content_sha256=article_evidence.content_sha256,
            article_evidence_sha256=article_evidence.evidence_sha256,
            media_evidence_sha256=media_evidence.media_evidence_sha256,
            metadata_only=metadata_only,
            files=tuple(sorted(files, key=lambda item: item.path)),
        ),
        image_artifacts,
    )


def _verify_bundle(staging: Path, manifest: EvidenceBundleManifest) -> None:
    parsed = EvidenceBundleManifest.model_validate_json(
        (staging / "manifest.json").read_text(encoding="utf-8")
    )
    if parsed != manifest:
        raise ValidationError("Evidence Bundle manifest verification failed.")
    expected = {"manifest.json"}
    for item in parsed.files:
        relative = PurePosixPath(item.path)
        path = staging.joinpath(*relative.parts)
        if not path.is_file() or _is_reparse_point(path):
            raise ValidationError("Evidence Bundle contains a missing or unsafe file.")
        if path.stat().st_size != item.byte_length or _file_sha256(path) != item.sha256:
            raise ValidationError("Evidence Bundle file hash verification failed.")
        expected.add(item.path)
    actual = {
        path.relative_to(staging).as_posix()
        for path in staging.rglob("*")
        if path.is_file()
    }
    if actual != expected:
        raise ValidationError("Evidence Bundle contains an unlisted file.")


def _bundle_file(root: Path, path: Path) -> BundleFile:
    return BundleFile(
        path=path.relative_to(root).as_posix(),
        byte_length=path.stat().st_size,
        sha256=_file_sha256(path),
    )


def _json_bytes(value: object) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, separators=(",", ": "))
        + "\n"
    ).encode("utf-8")


def _write_new_file(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bytes_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _revalidate_destination(destination: BundleDestination) -> None:
    _guard_path_components(destination.parent)
    if destination.parent.resolve(strict=True) != destination.parent:
        raise ValidationError("The Evidence Bundle parent directory changed after preflight.")
    if os.path.lexists(destination.path):
        raise ValidationError("The Evidence Bundle destination must not already exist.")


def _guard_path_components(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    components = absolute.parts[1:] if absolute.anchor else absolute.parts
    for component in components:
        current /= component
        if not os.path.lexists(current):
            raise ValidationError("The Evidence Bundle path contains a missing parent directory.")
        if _is_reparse_point(current):
            raise ValidationError(
                "The Evidence Bundle path cannot pass through a reparse point.",
                path=str(current),
            )


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    is_junction = getattr(path, "is_junction", lambda: False)
    return bool(
        path.is_symlink()
        or is_junction()
        or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _cleanup_staging(
    staging: Path,
    destination: BundleDestination,
    expected_prefix: str,
) -> None:
    if not os.path.lexists(staging):
        return
    try:
        if (
            staging.parent != destination.parent
            or not staging.name.startswith(expected_prefix)
            or _is_reparse_point(staging)
            or not staging.is_dir()
        ):
            return
        _guard_path_components(destination.parent)
        shutil.rmtree(staging)
    except (OSError, ValidationError):
        return
