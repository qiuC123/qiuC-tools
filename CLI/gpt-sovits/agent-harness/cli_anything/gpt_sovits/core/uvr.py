from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
import unicodedata
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse, urlunsplit

from cli_anything.gpt_sovits.core.errors import CLIError
from cli_anything.gpt_sovits.core.paths import require_local_path


APPROVED_UVR_URL = "https://huggingface.co/XXXXRT/GPT-SoVITS-Pretrained/resolve/main/uvr5_weights.zip"
MAX_ARCHIVE_BYTES = 4 * 1024**3
ALLOWED_FINAL_HOST_SUFFIXES = ("huggingface.co", "hf.co")
WINDOWS_DEVICE_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CLOCK$",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def validate_uvr_url(url: str) -> str:
    if url != APPROVED_UVR_URL:
        raise CLIError("uvr_url_forbidden", "只允许阶段 2A 已批准的官方 UVR5 包", {"approved_url": APPROVED_UVR_URL})
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "huggingface.co":
        raise CLIError("uvr_url_forbidden", "UVR5 下载必须使用获批的 Hugging Face HTTPS 地址")
    return url


def _redacted_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _validate_final_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not any(host == suffix or host.endswith(f".{suffix}") for suffix in ALLOWED_FINAL_HOST_SUFFIXES):
        raise CLIError("uvr_redirect_forbidden", "UVR5 下载重定向到了未批准的主机", {"final_url": _redacted_url(url)})
    return _redacted_url(url)


class _ApprovedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_final_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _default_transport(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": "qiuC-gpt-sovits-cli/phase-2a"})
    opener = urllib.request.build_opener(_ApprovedRedirectHandler())
    return opener.open(request, timeout=60)


def download_uvr_archive(
    url: str,
    output: str | Path,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
    overwrite: bool = False,
    dry_run: bool = False,
    transport=None,
) -> dict:
    validate_uvr_url(url)
    output_path = require_local_path(output, purpose="uvr_archive_output")
    if output_path.exists() and not overwrite:
        raise CLIError("output_exists", "UVR5 下载目标已存在；请显式使用 --overwrite", {"path": str(output_path)})
    if expected_size is not None and (expected_size <= 0 or expected_size > MAX_ARCHIVE_BYTES):
        raise CLIError("invalid_expected_size", "预期 UVR5 包大小无效")
    if expected_sha256 is not None:
        expected_sha256 = expected_sha256.lower()
        if len(expected_sha256) != 64 or any(character not in "0123456789abcdef" for character in expected_sha256):
            raise CLIError("invalid_expected_hash", "预期 SHA256 格式无效")
    plan = {"url": url, "output": str(output_path), "dry_run": dry_run, "overwrite": overwrite}
    if dry_run:
        return plan

    output_path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent)
    temp_path = Path(temp_name)
    os.close(handle)
    digest = hashlib.sha256()
    total = 0
    opener = transport or _default_transport
    try:
        with opener(url) as response, temp_path.open("wb") as target:
            final_url = _validate_final_url(response.geturl())
            declared = response.headers.get("Content-Length")
            if declared:
                try:
                    declared_size = int(declared)
                except ValueError as exc:
                    raise CLIError("invalid_content_length", "UVR5 下载响应大小无效") from exc
                if declared_size > MAX_ARCHIVE_BYTES:
                    raise CLIError("download_too_large", "UVR5 下载超过安全大小上限")
                if expected_size is not None and declared_size != expected_size:
                    raise CLIError("download_size_mismatch", "UVR5 下载响应大小与预期不一致")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise CLIError("download_too_large", "UVR5 下载超过安全大小上限")
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        actual_hash = digest.hexdigest()
        if expected_size is not None and total != expected_size:
            raise CLIError("download_size_mismatch", "UVR5 下载字节数与预期不一致", {"expected": expected_size, "actual": total})
        if expected_sha256 is not None and actual_hash != expected_sha256:
            raise CLIError("download_hash_mismatch", "UVR5 下载 SHA256 与预期不一致")
        if overwrite:
            os.replace(temp_path, output_path)
        else:
            try:
                os.link(temp_path, output_path)
            except FileExistsError as exc:
                raise CLIError("output_exists", "UVR5 下载目标在提交时已存在", {"path": str(output_path)}) from exc
            temp_path.unlink()
        return {**plan, "final_url": final_url, "bytes": total, "sha256": actual_hash}
    finally:
        temp_path.unlink(missing_ok=True)


def _safe_member(info: zipfile.ZipInfo) -> PurePosixPath:
    name = info.filename.replace("\\", "/")
    path = PurePosixPath(name)
    mode = info.external_attr >> 16
    components = name.split("/")
    unsafe_component = any(
        not component
        or ":" in component
        or component.endswith((".", " "))
        or component.split(".", 1)[0].upper() in WINDOWS_DEVICE_NAMES
        for component in components
    )
    if not name or path.is_absolute() or ".." in path.parts or unsafe_component or stat.S_ISLNK(mode):
        raise CLIError("unsafe_archive", "UVR5 ZIP 包含不安全条目", {"entry": info.filename})
    return path


def _normalized_member(path: PurePosixPath) -> str:
    return "/".join(unicodedata.normalize("NFKC", component).casefold() for component in path.parts)


def _copy_zip_member(stream: zipfile.ZipFile, info: zipfile.ZipInfo, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with stream.open(info) as source, destination.open("xb") as target:
        shutil.copyfileobj(source, target, length=1024 * 1024)


def _commit_staged_directory(stage: Path, output: Path) -> None:
    if not output.exists():
        os.replace(stage, output)
        return
    backup = output.parent / f".{output.name}.backup-{uuid.uuid4().hex}"
    os.replace(output, backup)
    try:
        os.replace(stage, output)
    except Exception:
        os.replace(backup, output)
        raise
    else:
        shutil.rmtree(backup)


def safe_extract_uvr_zip(archive: str | Path, output_dir: str | Path) -> dict:
    archive_path = require_local_path(archive, purpose="uvr_archive")
    output = require_local_path(output_dir, purpose="uvr_output")
    if not archive_path.is_file():
        raise CLIError("archive_not_found", "找不到 UVR5 ZIP 包", {"path": str(archive_path)})
    stage: Path | None = None
    try:
        with zipfile.ZipFile(archive_path) as stream:
            members = [(info, _safe_member(info)) for info in stream.infolist() if not info.is_dir()]
            strip_official_root = bool(members) and all(relative.parts[0] == "uvr5_weights" for _, relative in members)
            if strip_official_root:
                members = [(info, PurePosixPath(*relative.parts[1:])) for info, relative in members if len(relative.parts) > 1]
            members = [(info, relative) for info, relative in members if relative.name != ".gitignore"]
            normalized = [_normalized_member(relative) for _, relative in members]
            if len(normalized) != len(set(normalized)):
                raise CLIError("unsafe_archive", "UVR5 ZIP 包含大小写或 Unicode 规范化冲突条目")
            if not members or len(members) > 1000 or sum(info.file_size for info, _ in members) > MAX_ARCHIVE_BYTES:
                raise CLIError("unsafe_archive", "UVR5 ZIP 条目数量或解压大小异常")
            conflicts = [str(output / Path(*relative.parts)) for _, relative in members if (output / Path(*relative.parts)).exists()]
            if conflicts:
                raise CLIError("output_exists", "UVR5 权重目标已存在；不会隐式覆盖", {"paths": conflicts})
            output.parent.mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.stage-", dir=output.parent))
            if output.exists():
                shutil.rmtree(stage)
                shutil.copytree(output, stage, symlinks=False)
            for info, relative in members:
                _copy_zip_member(stream, info, stage / Path(*relative.parts))
            _commit_staged_directory(stage, output)
            stage = None
    except zipfile.BadZipFile as exc:
        raise CLIError("invalid_archive", "UVR5 下载不是有效 ZIP") from exc
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
    return {"archive": str(archive_path), "output_dir": str(output), "files": [relative.as_posix() for _, relative in members]}
