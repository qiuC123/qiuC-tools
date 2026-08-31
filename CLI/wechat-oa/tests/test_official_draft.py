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
        path.write_bytes(f"body-image-{index}".encode())
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
        if request.url.path == "/cgi-bin/draft/get":
            return httpx.Response(
                200,
                json={
                    "news_item": [
                        {
                            "title": "示例标题",
                            "content": '<img src="https://mmbiz.qpic.cn/body/0" />',
                        }
                    ]
                },
            )
        pytest.fail(f"unexpected endpoint: {request.url.path}")

    client = httpx.Client(transport=httpx.MockTransport(respond))
    result = OfficialDraftWriter(client, RetryTokens()).create(_prepared_draft(tmp_path))

    assert result.media_id == "draft-media"
    assert result.draft_created is True
    assert result.published is False
    assert result.verification.verified is True
    assert [request.url.path for request in requests] == [
        "/cgi-bin/media/uploadimg",
        "/cgi-bin/material/add_material",
        "/cgi-bin/draft/add",
        "/cgi-bin/draft/get",
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

    assert raised.value.details["errcode"] == 40009
    assert raised.value.details["uploaded_body_images"] == 1
    assert raised.value.details["cover_uploaded"] is False
    assert "checkpoint" in raised.value.details
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
    assert error.details["errcode"] == 48001
    assert error.details["uploaded_body_images"] == 1
    assert error.details["cover_uploaded"] is True
    assert "checkpoint" in error.details
    assert "api unauthorized" not in str(error)


def test_duplicate_images_resume_from_checkpoint_without_reupload(tmp_path: Path) -> None:
    draft = _prepared_draft(tmp_path, image_count=2)
    draft.images[1].path.write_bytes(draft.images[0].path.read_bytes())
    checkpoint_dir = tmp_path / "checkpoints"
    phase = 1
    paths: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal phase
        paths.append(request.url.path)
        if request.url.path == "/cgi-bin/media/uploadimg":
            return httpx.Response(200, json={"url": "https://mmbiz.qpic.cn/shared/0"})
        if request.url.path == "/cgi-bin/material/add_material" and phase == 1:
            phase = 2
            return httpx.Response(200, json={"errcode": 40009})
        if request.url.path == "/cgi-bin/material/add_material":
            return httpx.Response(200, json={"media_id": "cover-media"})
        if request.url.path == "/cgi-bin/draft/add":
            return httpx.Response(200, json={"media_id": "draft-media"})
        if request.url.path == "/cgi-bin/draft/get":
            return httpx.Response(
                200,
                json={
                    "news_item": [{
                        "title": "示例标题",
                        "content": (
                            '<img src="https://mmbiz.qpic.cn/shared/0" />'
                            '<img src="https://mmbiz.qpic.cn/shared/0" />'
                        ),
                    }]
                },
            )
        pytest.fail(request.url.path)

    writer = OfficialDraftWriter(
        httpx.Client(transport=httpx.MockTransport(respond)),
        RetryTokens(),
        checkpoint_dir,
    )
    with pytest.raises(ValidationError):
        writer.create(draft)
    result = writer.create(draft)

    assert paths.count("/cgi-bin/media/uploadimg") == 1
    assert result.uploaded_image_count == 0
    assert result.reused_image_count == 2
    checkpoint = next(checkpoint_dir.glob("*.json")).read_text(encoding="utf-8")
    assert "cached-token" not in checkpoint


def test_readback_mismatch_reports_created_media_id(tmp_path: Path) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/media/uploadimg":
            return httpx.Response(200, json={"url": "https://mmbiz.qpic.cn/body/0"})
        if request.url.path == "/cgi-bin/material/add_material":
            return httpx.Response(200, json={"media_id": "cover-media"})
        if request.url.path == "/cgi-bin/draft/add":
            return httpx.Response(200, json={"media_id": "draft-media"})
        if request.url.path == "/cgi-bin/draft/get":
            return httpx.Response(200, json={"news_item": [{"title": "被改写", "content": ""}]})
        pytest.fail(request.url.path)

    writer = OfficialDraftWriter(
        httpx.Client(transport=httpx.MockTransport(respond)),
        RetryTokens(),
        tmp_path / "checkpoints",
    )
    with pytest.raises(WxcliError, match="readback verification") as raised:
        writer.create(_prepared_draft(tmp_path))

    assert raised.value.details["media_id"] == "draft-media"
    assert raised.value.details["verification"]["verified"] is False


def test_readback_accepts_wechat_qpic_rendition_normalization() -> None:
    expected = "http://mmbiz.qpic.cn/mmbiz_png/asset-alpha/0?from=appmsg"
    actual = (
        "https://mmbiz.qpic.cn/mmbiz_png/asset-alpha/640"
        "?from=appmsg&wx_fmt=png"
    )
    writer = OfficialDraftWriter(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "news_item": [{
                            "title": "示例标题",
                            "content": f'<img src="{actual}" />',
                        }]
                    },
                )
            )
        ),
        RetryTokens(),
    )

    result = writer.verify(
        "draft-media",
        0,
        "示例标题",
        f'<img src="{expected}" />',
        [expected],
    )

    assert result.image_order_matches is True
    assert result.verified is True


@pytest.mark.parametrize(
    "actual_images",
    [
        ["https://mmbiz.qpic.cn/mmbiz_png/asset-other/640?from=appmsg"],
        ["https://mmbiz.qpic.cn.evil.test/mmbiz_png/asset-alpha/640?from=appmsg"],
        [
            "https://mmbiz.qpic.cn/mmbiz_png/asset-beta/640?from=appmsg",
            "https://mmbiz.qpic.cn/mmbiz_png/asset-alpha/640?from=appmsg",
        ],
    ],
)
def test_readback_rejects_different_qpic_asset_host_or_order(
    actual_images: list[str],
) -> None:
    expected_images = [
        "http://mmbiz.qpic.cn/mmbiz_png/asset-alpha/0?from=appmsg",
    ]
    if len(actual_images) == 2:
        expected_images.append(
            "http://mmbiz.qpic.cn/mmbiz_png/asset-beta/0?from=appmsg"
        )
    actual_content = "".join(f'<img src="{url}" />' for url in actual_images)
    writer = OfficialDraftWriter(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "news_item": [{
                            "title": "示例标题",
                            "content": actual_content,
                        }]
                    },
                )
            )
        ),
        RetryTokens(),
    )

    with pytest.raises(WxcliError, match="readback verification") as raised:
        writer.verify(
            "draft-media",
            0,
            "示例标题",
            "".join(f'<img src="{url}" />' for url in expected_images),
            expected_images,
        )

    assert raised.value.details["verification"]["image_order_matches"] is False


def test_update_refuses_stale_snapshot_before_uploading(tmp_path: Path) -> None:
    paths: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={"news_item": [{"title": "current", "content": "<p>changed</p>"}]},
        )

    writer = OfficialDraftWriter(
        httpx.Client(transport=httpx.MockTransport(respond)),
        RetryTokens(),
        tmp_path / "checkpoints",
    )
    with pytest.raises(ValidationError, match="changed after"):
        writer.update("draft-media", 0, _prepared_draft(tmp_path), "old-fingerprint")

    assert paths == ["/cgi-bin/draft/get"]


def test_snapshot_fingerprint_ignores_rotating_preview_url() -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "news_item": [{
                    "title": "未改变",
                    "content": "<p>正文</p>",
                    "url": f"https://mp.weixin.qq.com/s/temporary-{calls}",
                }]
            },
        )

    writer = OfficialDraftWriter(
        httpx.Client(transport=httpx.MockTransport(respond)),
        RetryTokens(),
    )

    first = writer.snapshot("draft-media")
    second = writer.snapshot("draft-media")

    assert first.news_items[0]["url"] != second.news_items[0]["url"]
    assert first.fingerprint == second.fingerprint


def test_snapshot_fingerprint_still_detects_editable_content_change() -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "news_item": [{
                    "title": "未改变",
                    "content": f"<p>正文版本 {calls}</p>",
                    "url": "https://mp.weixin.qq.com/s/temporary",
                }]
            },
        )

    writer = OfficialDraftWriter(
        httpx.Client(transport=httpx.MockTransport(respond)),
        RetryTokens(),
    )

    first = writer.snapshot("draft-media")
    second = writer.snapshot("draft-media")

    assert first.fingerprint != second.fingerprint


def test_update_rechecks_then_uploads_updates_and_verifies(tmp_path: Path) -> None:
    draft = _prepared_draft(tmp_path)
    requests: list[httpx.Request] = []
    initial: dict[str, object] = {
        "title": "old",
        "content": "<p>old</p>",
        "content_source_url": "https://example.com/source",
        "need_open_comment": 1,
        "only_fans_can_comment": 1,
    }
    update_sent = False

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal update_sent
        requests.append(request)
        if request.url.path == "/cgi-bin/draft/get" and not update_sent:
            return httpx.Response(200, json={"news_item": [initial]})
        if request.url.path == "/cgi-bin/media/uploadimg":
            return httpx.Response(200, json={"url": "https://mmbiz.qpic.cn/body/0"})
        if request.url.path == "/cgi-bin/material/add_material":
            return httpx.Response(200, json={"media_id": "cover-media"})
        if request.url.path == "/cgi-bin/draft/update":
            update_sent = True
            return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})
        if request.url.path == "/cgi-bin/draft/get":
            return httpx.Response(
                200,
                json={"news_item": [{
                    "title": "示例标题",
                    "content": '<img src="https://mmbiz.qpic.cn/body/0" />',
                }]},
            )
        pytest.fail(request.url.path)

    writer = OfficialDraftWriter(
        httpx.Client(transport=httpx.MockTransport(respond)),
        RetryTokens(),
        tmp_path / "checkpoints",
    )
    expected = writer.snapshot("draft-media").fingerprint
    requests.clear()
    result = writer.update("draft-media", 0, draft, expected)

    assert result.draft_created is False
    assert result.verification.verified is True
    assert [request.url.path for request in requests] == [
        "/cgi-bin/draft/get",
        "/cgi-bin/media/uploadimg",
        "/cgi-bin/material/add_material",
        "/cgi-bin/draft/get",
        "/cgi-bin/draft/update",
        "/cgi-bin/draft/get",
    ]
    update_body = json.loads(requests[4].content)
    assert update_body["media_id"] == "draft-media"
    assert update_body["index"] == 0
    assert update_body["articles"]["title"] == "示例标题"
    assert update_body["articles"]["content_source_url"] == "https://example.com/source"
    assert update_body["articles"]["need_open_comment"] == 1


def test_update_refuses_change_that_happens_during_upload(tmp_path: Path) -> None:
    first = {"news_item": [{"title": "old", "content": "<p>old</p>"}]}
    get_calls = 0
    paths: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal get_calls
        paths.append(request.url.path)
        if request.url.path == "/cgi-bin/draft/get":
            get_calls += 1
            if get_calls <= 2:
                return httpx.Response(200, json=first)
            return httpx.Response(
                200,
                json={"news_item": [{"title": "someone changed it", "content": "<p>new</p>"}]},
            )
        if request.url.path == "/cgi-bin/media/uploadimg":
            return httpx.Response(200, json={"url": "https://mmbiz.qpic.cn/body/0"})
        if request.url.path == "/cgi-bin/material/add_material":
            return httpx.Response(200, json={"media_id": "cover-media"})
        pytest.fail(request.url.path)

    writer = OfficialDraftWriter(
        httpx.Client(transport=httpx.MockTransport(respond)),
        RetryTokens(),
        tmp_path / "checkpoints",
    )
    expected = writer.snapshot("draft-media").fingerprint
    with pytest.raises(ValidationError, match="while prepared images"):
        writer.update("draft-media", 0, _prepared_draft(tmp_path), expected)

    assert "/cgi-bin/draft/update" not in paths


def test_stale_checkpoint_lock_is_recovered(tmp_path: Path) -> None:
    draft = _prepared_draft(tmp_path)
    checkpoint_dir = tmp_path / "checkpoints"
    writer = OfficialDraftWriter(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json=(
                        {"url": "https://mmbiz.qpic.cn/body/0"}
                        if request.url.path == "/cgi-bin/media/uploadimg"
                        else {"media_id": "cover-media"}
                        if request.url.path == "/cgi-bin/material/add_material"
                        else {"media_id": "draft-media"}
                        if request.url.path == "/cgi-bin/draft/add"
                        else {"news_item": [{
                            "title": "示例标题",
                            "content": '<img src="https://mmbiz.qpic.cn/body/0" />',
                        }]}
                    ),
                )
            )
        ),
        RetryTokens(),
        checkpoint_dir,
    )
    checkpoint = writer._checkpoint_path(draft)  # noqa: SLF001 - lock recovery contract
    checkpoint.parent.mkdir(parents=True)
    checkpoint.with_suffix(checkpoint.suffix + ".lock").write_text("crashed", encoding="ascii")

    result = writer.create(draft)

    assert result.verification.verified is True
