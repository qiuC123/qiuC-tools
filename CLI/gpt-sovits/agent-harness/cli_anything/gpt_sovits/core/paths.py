from __future__ import annotations

import ctypes
import os
from pathlib import Path

from cli_anything.gpt_sovits.core.errors import CLIError


DRIVE_REMOTE = 4


def _get_drive_type(root: str) -> int:
    if os.name != "nt":
        return 3
    return int(ctypes.windll.kernel32.GetDriveTypeW(str(root)))


def _windows_drive_root(value: str | Path) -> str | None:
    """Return a drive root using lexical parsing only (no filesystem access)."""
    windows_form = str(value).replace("/", "\\")
    if len(windows_form) >= 2 and windows_form[0].isalpha() and windows_form[1] == ":":
        return f"{windows_form[0].upper()}:\\"
    return None


def _reject_remote_drive(value: str | Path, *, purpose: str, reported_path: str) -> None:
    root = _windows_drive_root(value)
    if root and _get_drive_type(root) == DRIVE_REMOTE:
        raise CLIError(
            "nonlocal_path",
            "数据准备拒绝映射到网络位置的磁盘",
            {"purpose": purpose, "path": reported_path, "root": root},
        )


def require_local_path(path: str | Path, *, purpose: str) -> Path:
    """Resolve a path without ever accepting UNC or mapped network storage."""
    raw = str(path).strip()
    windows_form = raw.replace("/", "\\")
    if not raw or windows_form.startswith("\\\\"):
        raise CLIError("nonlocal_path", "数据准备只允许本机磁盘路径", {"purpose": purpose, "path": raw})

    # Expand user syntax without resolving or probing the filesystem, then reject
    # nonlocal homes and mapped drives before Path.resolve() can touch a provider.
    expanded = os.path.expanduser(raw)
    expanded_form = expanded.replace("/", "\\")
    if expanded_form.startswith("\\\\"):
        raise CLIError("nonlocal_path", "数据准备只允许本机磁盘路径", {"purpose": purpose, "path": raw})
    _reject_remote_drive(expanded, purpose=purpose, reported_path=raw)

    resolved = Path(expanded).resolve()
    resolved_form = str(resolved).replace("/", "\\")
    if resolved_form.startswith("\\\\"):
        raise CLIError("nonlocal_path", "数据准备只允许本机磁盘路径", {"purpose": purpose, "path": raw})
    # Resolve may traverse a reparse point or symlink, so validate the destination
    # independently even when the lexical input was a local or relative path.
    _reject_remote_drive(resolved, purpose=purpose, reported_path=raw)
    return resolved
