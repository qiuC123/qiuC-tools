"""Explicitly confirmed Official Account draft creation without publishing."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol, TypeVar
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from wxcli.auth import raise_for_official_error
from wxcli.draft_import import PreparedDraft
from wxcli.errors import ErrorCode, ValidationError, WxcliError

_UPLOAD_IMAGE = "https://api.weixin.qq.com/cgi-bin/media/uploadimg"
_ADD_MATERIAL = "https://api.weixin.qq.com/cgi-bin/material/add_material"
_DRAFT_ADD = "https://api.weixin.qq.com/cgi-bin/draft/add"

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
    draft_created: bool = True
    published: bool = False


class OfficialDraftWriter:
    """Upload prepared assets and create one new draft after explicit confirmation."""

    def __init__(self, client: httpx.Client, tokens: TokenProvider) -> None:
        self._client = client
        self._tokens = tokens

    def create(self, draft: PreparedDraft) -> DraftCreationResult:
        uploaded_urls: list[str] = []
        cover_uploaded = False
        try:
            for image in draft.images:
                uploaded_urls.append(self._upload_content_image(image.path))
            cover_media_id = self._upload_cover(draft.cover.path)
            cover_uploaded = True
            content = draft.content_with_urls(uploaded_urls)
            media_id = self._add_draft(draft, content, cover_media_id)
        except WxcliError as error:
            error.details.setdefault("uploaded_body_images", len(uploaded_urls))
            error.details.setdefault("cover_uploaded", cover_uploaded)
            raise
        return DraftCreationResult(
            media_id=media_id,
            title=draft.title,
            content_image_count=len(uploaded_urls),
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
        payload = self._json(_DRAFT_ADD, {"articles": [article]})
        value = payload.get("media_id")
        if not isinstance(value, str) or not _is_opaque_id(value):
            raise WxcliError(
                ErrorCode.PARSING_ERROR,
                "The Official Account API returned an invalid draft media_id.",
            )
        return value

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
