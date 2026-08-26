from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from docx import Document
from PIL import Image
from typer.testing import CliRunner

from wxcli.cli import app
from wxcli.draft_import import WordDraftImporter
from wxcli.errors import ValidationError


def _noise_png(path: Path, size: tuple[int, int]) -> None:
    image = Image.effect_noise(size, 80).convert("RGB")
    image.save(path, format="PNG")


def _word_article(path: Path, image_path: Path, *, with_table: bool = False) -> None:
    document = Document()
    document.add_heading("示例标题", level=0)
    document.sections[0].header.paragraphs[0].text = "不要导入页眉"
    document.sections[0].footer.paragraphs[0].text = "不要导入页脚"
    document.add_paragraph("第一段正文")
    document.add_picture(str(image_path))
    document.add_heading("金色小标题", level=1)
    document.add_paragraph("第二段正文")
    if with_table:
        document.add_table(rows=1, cols=1).cell(0, 0).text = "表格"
    document.save(path)


def test_word_draft_import_preserves_order_and_prepares_wechat_images(tmp_path: Path) -> None:
    source_image = tmp_path / "body.png"
    cover = tmp_path / "cover.png"
    source = tmp_path / "article.docx"
    output = tmp_path / "preview"
    _noise_png(source_image, (1920, 1080))
    _noise_png(cover, (1200, 510))
    _word_article(source, source_image)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    cover_hash = hashlib.sha256(cover.read_bytes()).hexdigest()

    prepared = WordDraftImporter().prepare(source, cover, output)

    assert prepared.title == "示例标题"
    assert len(prepared.images) == 1
    assert prepared.images[0].prepared_bytes < 1_000_000
    assert prepared.cover.prepared_bytes < 64_000
    assert prepared.content_template.index("第一段正文") < prepared.content_template.index(
        "wxcli-image-001"
    )
    assert prepared.content_template.index("wxcli-image-001") < prepared.content_template.index(
        "金色小标题"
    )
    assert "#9a6a22" in prepared.content_template
    assert "不要导入页眉" not in prepared.content_template
    assert "不要导入页脚" not in prepared.content_template
    assert Path(prepared.preview.preview_html).is_file()
    assert Path(prepared.preview.manifest).is_file()
    manifest = json.loads(Path(prepared.preview.manifest).read_text(encoding="utf-8"))
    assert manifest["ready_for_upload"] is True
    assert manifest["content_image_count"] == 1
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    assert hashlib.sha256(cover.read_bytes()).hexdigest() == cover_hash


def test_uploaded_urls_replace_every_placeholder(tmp_path: Path) -> None:
    source_image = tmp_path / "body.png"
    cover = tmp_path / "cover.png"
    source = tmp_path / "article.docx"
    _noise_png(source_image, (640, 360))
    _noise_png(cover, (1200, 510))
    _word_article(source, source_image)
    prepared = WordDraftImporter().prepare(source, cover, tmp_path / "preview")

    content = prepared.content_with_urls(["https://mmbiz.qpic.cn/example/0"])

    assert "wxcli-image-001" not in content
    assert 'src="https://mmbiz.qpic.cn/example/0"' in content


def test_nonempty_output_and_word_tables_are_rejected(tmp_path: Path) -> None:
    source_image = tmp_path / "body.png"
    cover = tmp_path / "cover.png"
    source = tmp_path / "article.docx"
    _noise_png(source_image, (640, 360))
    _noise_png(cover, (1200, 510))
    _word_article(source, source_image, with_table=True)
    output = tmp_path / "preview"
    output.mkdir()
    (output / "existing.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(ValidationError, match="must be empty"):
        WordDraftImporter().prepare(source, cover, output)
    assert (output / "existing.txt").read_text(encoding="utf-8") == "keep"

    with pytest.raises(ValidationError, match="tables are not supported"):
        WordDraftImporter().prepare(source, cover, tmp_path / "empty")


def test_cli_preview_is_local_and_emits_one_json_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_image = tmp_path / "body.png"
    cover = tmp_path / "cover.png"
    source = tmp_path / "article.docx"
    output = tmp_path / "preview"
    _noise_png(source_image, (640, 360))
    _noise_png(cover, (1200, 510))
    _word_article(source, source_image)

    def reject_network(*args: object, **kwargs: object) -> None:
        pytest.fail("local preview must not create an HTTP client")

    monkeypatch.setattr("wxcli.cli.httpx.Client", reject_network)
    result = CliRunner().invoke(
        app,
        [
            "--json",
            "account",
            "draft",
            "import-word",
            str(source),
            "--cover",
            str(cover),
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["ready_for_upload"] is True
    assert result.stderr == ""
