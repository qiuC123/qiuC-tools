"""Read-only planning and explicitly confirmed safe draft replacement."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field

from wxcli.draft_import import DraftImportPreview, PreparedDraft, WordDraftImporter
from wxcli.errors import ErrorCode, ValidationError, WxcliError
from wxcli.official_draft import DraftCreationResult, DraftSnapshot, OfficialDraftWriter


class DraftDifference(BaseModel):
    """A semantic, read-only summary of the planned article replacement."""

    model_config = ConfigDict(extra="forbid")

    changed: bool
    changes: list[str]
    current_title: str
    desired_title: str
    current_body_characters: int = Field(ge=0)
    desired_body_characters: int = Field(ge=0)
    current_image_count: int = Field(ge=0)
    desired_image_count: int = Field(ge=0)


class DraftBackupResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    media_id: str
    output: str
    fingerprint: str
    article_count: int = Field(ge=1)


class DraftUpdatePlan(BaseModel):
    """Local mutation plan that binds prepared files to one remote snapshot."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    media_id: str
    index: int = Field(ge=0)
    remote_fingerprint: str
    package_sha256: str
    prepared_directory: str = "prepared"
    backup_file: str = "backup.json"
    difference: DraftDifference
    preview: DraftImportPreview
    ready_for_update: bool = True


class DraftUpdatePlanner:
    """Create backups/plans without writing remotely, then apply a frozen plan."""

    def __init__(self, writer: OfficialDraftWriter) -> None:
        self._writer = writer

    def backup(self, media_id: str, output: Path) -> DraftBackupResult:
        snapshot = self._writer.snapshot(media_id)
        if output.exists():
            raise ValidationError("The draft backup output file already exists.")
        _atomic_json(output, snapshot.model_dump(mode="json"))
        return DraftBackupResult(
            media_id=media_id,
            output=str(output.resolve()),
            fingerprint=snapshot.fingerprint,
            article_count=len(snapshot.news_items),
        )

    def plan(
        self,
        media_id: str,
        index: int,
        source: Path,
        cover: Path,
        output_dir: Path,
        *,
        author: str | None = None,
        digest: str | None = None,
    ) -> DraftUpdatePlan:
        if index < 0:
            raise ValidationError("The article index must be at least 0.")
        _require_empty_directory(output_dir)
        existed = output_dir.exists()
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            snapshot = self._writer.snapshot(media_id)
            if index >= len(snapshot.news_items):
                raise ValidationError("The article index does not exist in this draft.")
            current = snapshot.news_items[index]
            effective_author = author if author is not None else _optional_text(current.get("author"))
            effective_digest = digest if digest is not None else _optional_text(current.get("digest"))
            prepared = WordDraftImporter().prepare(
                source,
                cover,
                output_dir / "prepared",
                author=effective_author,
                digest=effective_digest,
            )
            difference = _difference(current, prepared)
            _atomic_json(output_dir / "backup.json", snapshot.model_dump(mode="json"))
            plan = DraftUpdatePlan(
                media_id=media_id,
                index=index,
                remote_fingerprint=snapshot.fingerprint,
                package_sha256=prepared.package_sha256,
                difference=difference,
                preview=prepared.preview,
            )
            _atomic_json(output_dir / "plan.json", plan.model_dump(mode="json"))
            return plan
        except Exception:
            if not existed and output_dir.exists():
                shutil.rmtree(output_dir, ignore_errors=True)
            raise

    def apply(self, plan_dir: Path, *, confirmed: bool) -> DraftCreationResult:
        if not confirmed:
            raise ValidationError("Applying a draft update requires --confirm.")
        root = plan_dir.expanduser().resolve()
        try:
            plan = DraftUpdatePlan.model_validate_json(
                (root / "plan.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as error:
            raise ValidationError("The draft update plan could not be read.") from error
        prepared = PreparedDraft.load(root / plan.prepared_directory)
        if prepared.package_sha256 != plan.package_sha256:
            raise ValidationError("The prepared draft differs from the update plan.")
        return self._writer.update(
            plan.media_id,
            plan.index,
            prepared,
            plan.remote_fingerprint,
        )


def _difference(current: dict[str, Any], desired: PreparedDraft) -> DraftDifference:
    title_value = current.get("title")
    content_value = current.get("content")
    current_title: str = title_value if isinstance(title_value, str) else ""
    current_content: str = content_value if isinstance(content_value, str) else ""
    current_text = _text(current_content)
    desired_text = _text(desired.content_template)
    current_images = len(_images(current_content))
    changes: list[str] = []
    if current_title != desired.title:
        changes.append("title")
    if current_text != desired_text:
        changes.append("body_text")
    if current_images or desired.images:
        changes.append("body_images")
    if (current.get("author") or None) != desired.author:
        changes.append("author")
    if (current.get("digest") or None) != desired.digest:
        changes.append("digest")
    return DraftDifference(
        changed=bool(changes),
        changes=changes,
        current_title=current_title,
        desired_title=desired.title,
        current_body_characters=len(current_text),
        desired_body_characters=len(desired_text),
        current_image_count=current_images,
        desired_image_count=len(desired.images),
    )


def _text(value: str) -> str:
    return " ".join(BeautifulSoup(value, "lxml").get_text(" ", strip=True).split())


def _images(value: str) -> list[str]:
    return [
        str(image.get("data-src") or image.get("src"))
        for image in BeautifulSoup(value, "lxml").select("img")
        if image.get("data-src") or image.get("src")
    ]


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _atomic_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise WxcliError(
            ErrorCode.LOCAL_CONFIGURATION_ERROR,
            "The draft plan or backup could not be written.",
        ) from error


def _require_empty_directory(path: Path) -> None:
    try:
        if path.exists() and (not path.is_dir() or any(path.iterdir())):
            raise ValidationError("The draft update plan directory must be empty.")
    except ValidationError:
        raise
    except OSError as error:
        raise WxcliError(
            ErrorCode.LOCAL_CONFIGURATION_ERROR,
            "The draft update plan directory could not be inspected.",
        ) from error
