"""Explicitly confirmed Official Account draft creation without publishing."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterator, Protocol, TypeVar
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field

from wxcli.auth import raise_for_official_error
from wxcli.draft_import import PreparedDraft
from wxcli.errors import ErrorCode, NotFoundError, ValidationError, WxcliError

_UPLOAD_IMAGE = "https://api.weixin.qq.com/cgi-bin/media/uploadimg"
_ADD_MATERIAL = "https://api.weixin.qq.com/cgi-bin/material/add_material"
_DRAFT_ADD = "https://api.weixin.qq.com/cgi-bin/draft/add"
_DRAFT_GET = "https://api.weixin.qq.com/cgi-bin/draft/get"
_DRAFT_UPDATE = "https://api.weixin.qq.com/cgi-bin/draft/update"

T = TypeVar("T")


class TokenProvider(Protocol):
    """The controlled one-refresh retry operation required by this writer."""

    def with_token_retry(self, call: Callable[[str], T]) -> T: ...


class DraftCreationResult(BaseModel):
    """Safe result returned after creating, but never publishing, one draft."""

    model_config = ConfigDict(extra="forbid")

    media_id: str
    title: str
    content_image_count: int = Field(ge=0)
    uploaded_image_count: int = Field(default=0, ge=0)
    reused_image_count: int = Field(default=0, ge=0)
    verification: DraftVerification
    draft_created: bool = True
    published: bool = False


class DraftVerification(BaseModel):
    """Result of reading a mutation back from the Official Account API."""

    model_config = ConfigDict(extra="forbid")

    title_matches: bool
    body_text_matches: bool
    image_order_matches: bool
    expected_image_count: int = Field(ge=0)
    actual_image_count: int = Field(ge=0)
    verified: bool


class DraftSnapshot(BaseModel):
    """Exact API draft payload and a stable fingerprint for safe updates."""

    model_config = ConfigDict(extra="forbid")

    media_id: str
    news_items: list[dict[str, Any]] = Field(min_length=1)
    fingerprint: str


class UploadCheckpoint(BaseModel):
    """Non-secret resumable image-upload state."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    package_sha256: str
    body_images: dict[str, str] = Field(default_factory=dict)
    cover_sha256: str | None = None
    cover_media_id: str | None = None
    created_media_id: str | None = None


class OfficialDraftWriter:
    """Upload prepared assets and create one new draft after explicit confirmation."""

    def __init__(
        self,
        client: httpx.Client,
        tokens: TokenProvider,
        checkpoint_dir: Path | None = None,
    ) -> None:
        self._client = client
        self._tokens = tokens
        self._checkpoint_dir = checkpoint_dir

    def create(self, draft: PreparedDraft) -> DraftCreationResult:
        checkpoint_path = self._checkpoint_path(draft)
        uploaded_urls: list[str] = []
        uploaded_count = 0
        reused_count = 0
        cover_uploaded = False
        with self._checkpoint_lock(checkpoint_path):
            checkpoint = self._load_checkpoint(checkpoint_path, draft.package_sha256)
            try:
                for image in draft.images:
                    digest = _file_sha256(image.path)
                    existing = checkpoint.body_images.get(digest)
                    if existing is not None:
                        uploaded_urls.append(existing)
                        reused_count += 1
                    else:
                        url = self._upload_content_image(image.path)
                        uploaded_urls.append(url)
                        checkpoint.body_images[digest] = url
                        uploaded_count += 1
                        self._save_checkpoint(checkpoint_path, checkpoint)
                cover_digest = _file_sha256(draft.cover.path)
                if checkpoint.cover_sha256 == cover_digest and checkpoint.cover_media_id:
                    cover_media_id = checkpoint.cover_media_id
                else:
                    cover_media_id = self._upload_cover(draft.cover.path)
                    cover_uploaded = True
                    checkpoint.cover_sha256 = cover_digest
                    checkpoint.cover_media_id = cover_media_id
                    self._save_checkpoint(checkpoint_path, checkpoint)
                content = draft.content_with_urls(uploaded_urls)
                if checkpoint.created_media_id:
                    media_id = checkpoint.created_media_id
                else:
                    media_id = self._add_draft(draft, content, cover_media_id)
                    checkpoint.created_media_id = media_id
                    self._save_checkpoint(checkpoint_path, checkpoint)
                verification = self.verify(media_id, 0, draft.title, content, uploaded_urls)
            except WxcliError as error:
                error.details.setdefault("uploaded_body_images", uploaded_count)
                error.details.setdefault("reused_body_images", reused_count)
                error.details.setdefault("cover_uploaded", cover_uploaded)
                error.details.setdefault("checkpoint", str(checkpoint_path))
                raise
        return DraftCreationResult(
            media_id=media_id,
            title=draft.title,
            content_image_count=len(uploaded_urls),
            uploaded_image_count=uploaded_count,
            reused_image_count=reused_count,
            verification=verification,
        )

    def snapshot(self, media_id: str) -> DraftSnapshot:
        """Read the exact draft representation used by backup and concurrency checks."""
        if not _is_opaque_id(media_id):
            raise ValidationError("The media_id is invalid.")
        payload = self._json(_DRAFT_GET, {"media_id": media_id})
        items = payload.get("news_item")
        if not isinstance(items, list) or not items or not all(isinstance(item, dict) for item in items):
            raise WxcliError(ErrorCode.PARSING_ERROR, "The draft snapshot is invalid.")
        news_items = [dict(item) for item in items]
        return DraftSnapshot(
            media_id=media_id,
            news_items=news_items,
            fingerprint=_draft_fingerprint(news_items),
        )

    def verify(
        self,
        media_id: str,
        index: int,
        title: str,
        content: str,
        image_urls: list[str],
    ) -> DraftVerification:
        snapshot = self.snapshot(media_id)
        if not 0 <= index < len(snapshot.news_items):
            raise WxcliError(
                ErrorCode.PARSING_ERROR,
                "The changed draft article could not be found during verification.",
                details={"media_id": media_id, "index": index},
            )
        article = snapshot.news_items[index]
        actual_content = article.get("content")
        if not isinstance(actual_content, str):
            raise WxcliError(ErrorCode.PARSING_ERROR, "The changed draft body is invalid.")
        actual_images = _html_images(actual_content)
        result = DraftVerification(
            title_matches=article.get("title") == title,
            body_text_matches=_html_text(actual_content) == _html_text(content),
            image_order_matches=_image_sequences_match(actual_images, image_urls),
            expected_image_count=len(image_urls),
            actual_image_count=len(actual_images),
            verified=False,
        )
        result.verified = (
            result.title_matches and result.body_text_matches and result.image_order_matches
        )
        if not result.verified:
            raise WxcliError(
                ErrorCode.PARSING_ERROR,
                "The draft was changed, but its readback verification did not match.",
                details={
                    "media_id": media_id,
                    "index": index,
                    "verification": result.model_dump(mode="json"),
                },
            )
        return result

    def update(
        self,
        media_id: str,
        index: int,
        draft: PreparedDraft,
        expected_fingerprint: str,
    ) -> DraftCreationResult:
        """Replace one article only when the planned remote fingerprint still matches."""
        if index < 0:
            raise ValidationError("The article index must be at least 0.")
        current = self.snapshot(media_id)
        if current.fingerprint != expected_fingerprint:
            raise ValidationError("The remote draft changed after the update plan was created.")
        if index >= len(current.news_items):
            raise ValidationError("The article index does not exist in this draft.")
        checkpoint_path = self._checkpoint_path(draft, suffix=f"update-{_short_hash(media_id)}-{index}")
        uploaded_count = 0
        reused_count = 0
        with self._checkpoint_lock(checkpoint_path):
            checkpoint = self._load_checkpoint(checkpoint_path, draft.package_sha256)
            urls: list[str] = []
            for image in draft.images:
                digest = _file_sha256(image.path)
                if digest in checkpoint.body_images:
                    urls.append(checkpoint.body_images[digest])
                    reused_count += 1
                else:
                    url = self._upload_content_image(image.path)
                    checkpoint.body_images[digest] = url
                    urls.append(url)
                    uploaded_count += 1
                    self._save_checkpoint(checkpoint_path, checkpoint)
            cover_digest = _file_sha256(draft.cover.path)
            if checkpoint.cover_sha256 == cover_digest and checkpoint.cover_media_id:
                cover_media_id = checkpoint.cover_media_id
            else:
                cover_media_id = self._upload_cover(draft.cover.path)
                checkpoint.cover_sha256 = cover_digest
                checkpoint.cover_media_id = cover_media_id
                self._save_checkpoint(checkpoint_path, checkpoint)
            content = draft.content_with_urls(urls)
            latest = self.snapshot(media_id)
            if latest.fingerprint != expected_fingerprint:
                raise ValidationError("The remote draft changed while prepared images were uploading.")
            article = self._article_body(
                draft,
                content,
                cover_media_id,
                existing=latest.news_items[index],
            )
            self._json(_DRAFT_UPDATE, {"media_id": media_id, "index": index, "articles": article})
            verification = self.verify(media_id, index, draft.title, content, urls)
        return DraftCreationResult(
            media_id=media_id,
            title=draft.title,
            content_image_count=len(urls),
            uploaded_image_count=uploaded_count,
            reused_image_count=reused_count,
            verification=verification,
            draft_created=False,
        )

    def _upload_content_image(self, path: Path) -> str:
        payload = self._multipart(_UPLOAD_IMAGE, path, {})
        value = payload.get("url")
        if not isinstance(value, str) or not _is_http_url(value):
            raise WxcliError(
                ErrorCode.PARSING_ERROR,
                "The Official Account API returned an invalid article image URL.",
            )
        return value

    def _upload_cover(self, path: Path) -> str:
        payload = self._multipart(_ADD_MATERIAL, path, {"type": "thumb"})
        value = payload.get("media_id")
        if not isinstance(value, str) or not _is_opaque_id(value):
            raise WxcliError(
                ErrorCode.PARSING_ERROR,
                "The Official Account API returned an invalid cover media_id.",
            )
        return value

    def _add_draft(self, draft: PreparedDraft, content: str, cover_media_id: str) -> str:
        article = self._article_body(draft, content, cover_media_id)
        payload = self._json(_DRAFT_ADD, {"articles": [article]})
        value = payload.get("media_id")
        if not isinstance(value, str) or not _is_opaque_id(value):
            raise WxcliError(
                ErrorCode.PARSING_ERROR,
                "The Official Account API returned an invalid draft media_id.",
            )
        return value

    @staticmethod
    def _article_body(
        draft: PreparedDraft,
        content: str,
        cover_media_id: str,
        *,
        existing: Mapping[str, Any] | None = None,
    ) -> dict[str, object]:
        article: dict[str, object] = {
            "article_type": "news",
            "title": draft.title,
            "content": content,
            "thumb_media_id": cover_media_id,
            "need_open_comment": 0,
            "only_fans_can_comment": 0,
            "cover_info": {
                "crop_percent_list": [
                    {"ratio": "2.35_1", "x1": "0", "y1": "0", "x2": "1", "y2": "1"}
                ]
            },
        }
        if draft.author is not None:
            article["author"] = draft.author
        if draft.digest is not None:
            article["digest"] = draft.digest
        if existing is not None:
            source_url = existing.get("content_source_url")
            if isinstance(source_url, str) and source_url:
                article["content_source_url"] = source_url
            for setting in ("need_open_comment", "only_fans_can_comment"):
                value = existing.get(setting)
                if isinstance(value, int) and value in {0, 1}:
                    article[setting] = value
        return article

    def _checkpoint_path(self, draft: PreparedDraft, *, suffix: str = "create") -> Path:
        root = self._checkpoint_dir
        if root is None:
            manifest = Path(draft.preview.manifest) if draft.preview.manifest else draft.cover.path
            root = manifest.parent / ".wxcli-upload"
        return root / f"{draft.package_sha256}-{suffix}.json"

    @staticmethod
    def _load_checkpoint(path: Path, package_sha256: str) -> UploadCheckpoint:
        try:
            if not path.exists():
                return UploadCheckpoint(package_sha256=package_sha256)
            checkpoint = UploadCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise WxcliError(
                ErrorCode.LOCAL_CONFIGURATION_ERROR,
                "The upload checkpoint could not be read.",
            ) from error
        if checkpoint.package_sha256 != package_sha256:
            raise ValidationError("The upload checkpoint belongs to a different draft package.")
        return checkpoint

    @staticmethod
    def _save_checkpoint(path: Path, checkpoint: UploadCheckpoint) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                checkpoint.model_dump_json(indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise WxcliError(
                ErrorCode.LOCAL_CONFIGURATION_ERROR,
                "The upload checkpoint could not be saved.",
            ) from error

    @staticmethod
    @contextmanager
    def _checkpoint_lock(path: Path) -> Iterator[None]:
        lock_path = path.with_suffix(path.suffix + ".lock")
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise WxcliError(
                ErrorCode.LOCAL_CONFIGURATION_ERROR,
                "The upload checkpoint lock directory could not be created.",
            ) from error
        for attempt in range(2):
            try:
                descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(descriptor, str(os.getpid()).encode("ascii"))
                finally:
                    os.close(descriptor)
                break
            except FileExistsError as error:
                if attempt == 0 and _lock_is_stale(lock_path):
                    try:
                        lock_path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                raise WxcliError(
                    ErrorCode.LOCAL_CONFIGURATION_ERROR,
                    "Another wxcli process is using this draft upload checkpoint.",
                ) from error
            except OSError as error:
                raise WxcliError(
                    ErrorCode.LOCAL_CONFIGURATION_ERROR,
                    "The upload checkpoint lock could not be created.",
                ) from error
        try:
            yield
        finally:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError as error:
                raise WxcliError(
                    ErrorCode.LOCAL_CONFIGURATION_ERROR,
                    "The upload checkpoint lock could not be released.",
                ) from error

    def _multipart(
        self,
        url: str,
        path: Path,
        extra_params: Mapping[str, str],
    ) -> dict[str, Any]:
        return self._tokens.with_token_retry(
            lambda token: self._multipart_request(url, path, token, extra_params)
        )

    def _multipart_request(
        self,
        url: str,
        path: Path,
        token: str,
        extra_params: Mapping[str, str],
    ) -> dict[str, Any]:
        params = {"access_token": token, **dict(extra_params)}
        try:
            with path.open("rb") as stream:
                response = self._client.post(
                    url,
                    params=params,
                    files={"media": (path.name, stream, "image/jpeg")},
                )
        except (OSError, httpx.HTTPError) as error:
            raise WxcliError(
                ErrorCode.NETWORK_ERROR,
                "A prepared draft image could not be uploaded.",
            ) from error
        return _response_payload(response)

    def _json(self, url: str, body: Mapping[str, object]) -> dict[str, Any]:
        return self._tokens.with_token_retry(
            lambda token: self._json_request(url, body, token)
        )

    def _json_request(
        self,
        url: str,
        body: Mapping[str, object],
        token: str,
    ) -> dict[str, Any]:
        try:
            response = self._client.post(
                url,
                params={"access_token": token},
                json=dict(body),
            )
        except httpx.HTTPError as error:
            raise WxcliError(
                ErrorCode.NETWORK_ERROR,
                "The new draft could not be sent to the Official Account API.",
            ) from error
        try:
            payload = response.json()
        except ValueError:
            return _response_payload(response)
        if isinstance(payload, dict):
            errcode = payload.get("errcode")
            if url == _DRAFT_GET and errcode == 40007:
                raise NotFoundError("The requested draft was not found.")
            if url == _DRAFT_UPDATE and errcode in {40114, 41039, 45166}:
                raise ValidationError("WeChat rejected the draft article update.", errcode=errcode)
        return _response_payload(response)


def _response_payload(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as error:
        raise WxcliError(
            ErrorCode.NETWORK_ERROR,
            "The Official Account API returned invalid JSON.",
        ) from error
    if not isinstance(payload, dict):
        raise WxcliError(
            ErrorCode.PARSING_ERROR,
            "The Official Account API returned an unexpected object.",
        )
    errcode = payload.get("errcode")
    if errcode in {40005, 40009}:
        raise ValidationError(
            "WeChat rejected a prepared image type or size.",
            errcode=errcode,
        )
    raise_for_official_error(payload)
    if response.status_code != 200:
        raise WxcliError(
            ErrorCode.NETWORK_ERROR,
            "The Official Account API returned an HTTP error.",
        )
    return payload


def _is_http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.casefold() in {"http", "https"}
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and port is None
    )


def _is_opaque_id(value: str) -> bool:
    return (
        1 <= len(value) <= 512
        and value == value.strip()
        and all(character.isprintable() for character in value)
    )


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise WxcliError(
            ErrorCode.LOCAL_CONFIGURATION_ERROR,
            "A prepared draft image could not be read.",
        ) from error


def _json_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _draft_fingerprint(news_items: list[dict[str, Any]]) -> str:
    stable_items = [
        {key: value for key, value in item.items() if key != "url"}
        for item in news_items
    ]
    return _json_sha256(stable_items)


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _html_text(value: str) -> str:
    text = BeautifulSoup(value, "lxml").get_text(" ", strip=True)
    return " ".join(text.split())


def _html_images(value: str) -> list[str]:
    soup = BeautifulSoup(value, "lxml")
    images: list[str] = []
    for image in soup.select("img"):
        source = image.get("data-src") or image.get("src")
        if isinstance(source, str):
            images.append(source)
    return images


def _image_sequences_match(actual: list[str], expected: list[str]) -> bool:
    return len(actual) == len(expected) and all(
        _image_url_matches(actual_url, expected_url)
        for actual_url, expected_url in zip(actual, expected, strict=True)
    )


def _image_url_matches(actual: str, expected: str) -> bool:
    if actual == expected:
        return True
    try:
        actual_url = urlsplit(actual)
        expected_url = urlsplit(expected)
        actual_port = actual_url.port
        expected_port = expected_url.port
    except ValueError:
        return False
    if not all(
        parsed.scheme.casefold() in {"http", "https"}
        and parsed.hostname == "mmbiz.qpic.cn"
        and parsed.username is None
        and parsed.password is None
        for parsed in (actual_url, expected_url)
    ):
        return False
    if actual_port is not None or expected_port is not None:
        return False
    actual_path = actual_url.path.rstrip("/").split("/")
    expected_path = expected_url.path.rstrip("/").split("/")
    return (
        len(actual_path) >= 3
        and len(expected_path) >= 3
        and actual_path[-1].isdecimal()
        and expected_path[-1].isdecimal()
        and actual_path[:-1] == expected_path[:-1]
    )


def _lock_is_stale(path: Path) -> bool:
    try:
        process_id = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return True
    if process_id <= 0:
        return True
    try:
        os.kill(process_id, 0)
    except OverflowError:
        return True
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except OSError:
        return False
    return False
