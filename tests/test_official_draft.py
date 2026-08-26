from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import httpx
import pytest

from wxcli.auth import AccessTokenInvalid
from wxcli.draft_import import DraftImportPreview, PreparedDraft, PreparedImage
from wxcli.errors import ValidationError, WxcliError
from wxcli.official_draft import OfficialDraftWriter

T = TypeVar("T")


class RetryTokens:
    def with_token_retry(self, call: Callable[[str], T]) -> T:
        try:
            return call("cached-token")
        except AccessTokenInvalid:
            return call("refreshed-token")


def _prepared_draft(tmp_path: Path, image_count: int = 1) -> PreparedDraft:
    images: list[PreparedImage] = []
    placeholders: list[str] = []
    for index in range(1, image_count + 1):
        path = tmp_path / f"body-{index}.jpg"
        path.write_bytes(b"body-image")
        images.append(PreparedImage(path, 10, 10, 100, 100))
        placeholders.append(f'<img src="wxcli-image-{index:03d}" />')
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"cover-image")
    preview = DraftImportPreview(
        title="示例标题",
        source_docx=str(tmp_path / "source.docx"),
        cover_source=str(tmp_path / "cover.png"),
        preview_html=str(tmp_path / "preview.html"),
        manifest=str(tmp_path / "manifest.json"),
        content_image_count=image_count,
        original_image_bytes=image_count * 10,
        prepared_image_bytes=image_count * 10,
        content_characters=sum(len(value) for value in placeholders),
    )
    return PreparedDraft(
        title="示例标题",
        author=None,
        digest=None,
        content_template="\n".join(placeholders),
        images=tuple(images),
        cover=PreparedImage(cover, 11, 11, 1200, 510),
        preview=preview,
    )


def test_writer_uploads_body_then_cover_and_only_creates_draft(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/cgi-bin/media/uploadimg":
            return httpx.Response(
                200,
                json={"url": "https://mmbiz.qpic.cn/body/0", "errcode": 0, "errmsg": "ok"},
            )
        if request.url.path == "/cgi-bin/material/add_material":
            return httpx.Response(200, json={"media_id": "cover-media", "url": "cover-url"})
        if request.url.path == "/cgi-bin/draft/add":
            return httpx.Response(200, json={"media_id": "draft-media"})
        pytest.fail(f"unexpected endpoint: {request.url.path}")

    client = httpx.Client(transport=httpx.MockTransport(respond))
    result = OfficialDraftWriter(client, RetryTokens()).create(_prepared_draft(tmp_path))

    assert result.media_id == "draft-media"
    assert result.draft_created is True
    assert result.published is False
    assert [request.url.path for request in requests] == [
        "/cgi-bin/media/uploadimg",
        "/cgi-bin/material/add_material",
        "/cgi-bin/draft/add",
    ]
    assert requests[1].url.params["type"] == "thumb"
    draft_body = json.loads(requests[2].content)
    article = draft_body["articles"][0]
    assert article["thumb_media_id"] == "cover-media"
    assert "https://mmbiz.qpic.cn/body/0" in article["content"]
    assert article["need_open_comment"] == 0
    assert article["only_fans_can_comment"] == 0


def test_partial_upload_count_is_reported_without_server_text(tmp_path: Path) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"url": "https://mmbiz.qpic.cn/first/0"})
        return httpx.Response(200, json={"errcode": 40009, "errmsg": "sensitive server text"})

    client = httpx.Client(transport=httpx.MockTransport(respond))

    with pytest.raises(ValidationError) as raised:
        OfficialDraftWriter(client, RetryTokens()).create(_prepared_draft(tmp_path, image_count=2))

    assert raised.value.details == {
        "errcode": 40009,
        "uploaded_body_images": 1,
        "cover_uploaded": False,
    }
    assert "server text" not in raised.value.message


def test_failure_after_cover_reports_that_nonrollbackable_upload(tmp_path: Path) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/media/uploadimg":
            return httpx.Response(200, json={"url": "https://mmbiz.qpic.cn/body/0"})
        if request.url.path == "/cgi-bin/material/add_material":
            return httpx.Response(200, json={"media_id": "cover-media"})
        return httpx.Response(200, json={"errcode": 48001, "errmsg": "api unauthorized"})

    client = httpx.Client(transport=httpx.MockTransport(respond))

    with pytest.raises(WxcliError) as raised:
        OfficialDraftWriter(client, RetryTokens()).create(_prepared_draft(tmp_path))

    error = raised.value
    assert getattr(error, "details") == {
        "errcode": 48001,
        "uploaded_body_images": 1,
        "cover_uploaded": True,
    }
    assert "api unauthorized" not in str(error)
