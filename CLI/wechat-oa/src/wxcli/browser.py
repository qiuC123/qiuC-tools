"""Safe local state and locking for the dedicated wxcli Chrome profile."""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from wxcli.errors import ErrorCode, WxcliError


@dataclass(frozen=True, slots=True)
class BrowserStatus:
    """Local facts about the dedicated profile; never a session-validity claim."""

    profile_exists: bool
    last_successful_read_at: datetime | None
    legacy_last_verified_at: datetime | None

    @property
    def last_verified_at(self) -> datetime | None:
        """Compatibility alias for the explicitly legacy observation."""
        return self.legacy_last_verified_at


class ProfileLock:
    """A small cross-process exclusive lock beside the dedicated profile."""

    def __init__(
        self,
        profile: Path,
        *,
        timeout: float = 0.0,
        poll_interval: float = 0.05,
    ) -> None:
        self.path = profile.with_name(f"{profile.name}.lock")
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._held = False

    def __enter__(self) -> ProfileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError as error:
                if time.monotonic() >= deadline:
                    raise WxcliError(
                        ErrorCode.BROWSER_BUSY,
                        "The wxcli Chrome profile is already in use.",
                    ) from error
                time.sleep(min(self.poll_interval, max(0.0, deadline - time.monotonic())))
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(str(os.getpid()))
        self._held = True
        return self

    def __exit__(self, *_: object) -> None:
        if self._held:
            self.path.unlink(missing_ok=True)
            self._held = False


class BrowserProfile:
    """Own the local-only files for one independent wxcli browser profile."""

    def __init__(self, profile: Path, state: Path) -> None:
        self.profile = profile
        self.state = state

    def status(self) -> BrowserStatus:
        """Read profile facts without launching Chrome."""
        last_successful_read_at: datetime | None = None
        legacy_last_verified_at: datetime | None = None
        try:
            payload = json.loads(self.state.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("invalid browser state")
            successful = payload.get("last_successful_read_at")
            legacy = payload.get("legacy_last_verified_at")
            old = payload.get("last_verified_at")
            if isinstance(successful, str):
                last_successful_read_at = datetime.fromisoformat(successful)
            if isinstance(legacy, str):
                legacy_last_verified_at = datetime.fromisoformat(legacy)
            elif isinstance(old, str):
                legacy_last_verified_at = datetime.fromisoformat(old)
                self._write_state(last_successful_read_at, legacy_last_verified_at)
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            pass
        return BrowserStatus(
            self.profile.exists(),
            last_successful_read_at,
            legacy_last_verified_at,
        )

    def record_successful_read(self) -> None:
        status = self.status()
        self._write_state(datetime.now(UTC), status.legacy_last_verified_at)

    def _write_state(
        self,
        last_successful_read_at: datetime | None,
        legacy_last_verified_at: datetime | None,
    ) -> None:
        temporary = self.state.with_name(f".{self.state.name}.{uuid4().hex}.tmp")
        payload = {
            "schema_version": "1",
            "last_successful_read_at": (
                last_successful_read_at.isoformat() if last_successful_read_at else None
            ),
            "legacy_last_verified_at": (
                legacy_last_verified_at.isoformat() if legacy_last_verified_at else None
            ),
        }
        try:
            self.state.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(temporary, self.state)
        except OSError as error:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise WxcliError(
                ErrorCode.LOCAL_CONFIGURATION_ERROR,
                "The browser status could not be saved.",
            ) from error

    def clear(self) -> None:
        with ProfileLock(self.profile):
            if self.profile.exists():
                shutil.rmtree(self.profile)
            self.state.unlink(missing_ok=True)
