from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def _exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        locked = False
        try:
            try:
                if os.name == "nt":
                    import msvcrt

                    if stream.tell() == 0:
                        stream.write(b"0")
                        stream.flush()
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                locked = True
            except OSError:
                locked = False
            yield
        finally:
            if locked:
                try:
                    stream.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass


def locked_save_json(path: Path, data: dict) -> None:
    """Write JSON atomically while serializing writers with a sidecar lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _exclusive_lock(lock_path):
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def locked_append_json_line(path: Path, data: dict) -> None:
    """Append one UTF-8 JSON lifecycle event while serializing writers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _exclusive_lock(lock_path):
        with path.open("a", encoding="utf-8", newline="") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
