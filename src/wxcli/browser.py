"""Safe local state and locking for the dedicated wxcli Chrome profile."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from wxcli.errors import ErrorCode, WxcliError


@dataclass(frozen=True, slots=True)
class BrowserStatus:
    """Local facts about the dedicated profile; never a session-validity claim."""

    profile_exists: bool
    last_verified_at: datetime | None


class ProfileLock:
    """A small cross-process exclusive lock beside the dedicated profile."""

    def __init__(self, profile: Path) -> None:
        self.path = profile.with_name(f"{profile.name}.lock")
        self._held = False

    def __enter__(self) -> ProfileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as error:
            raise WxcliError(ErrorCode.CHROME_ERROR, "The wxcli Chrome profile is already in use.") from error
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
        last_verified_at: datetime | None = None
        try:
            value = json.loads(self.state.read_text(encoding="utf-8")).get("last_verified_at")
            if isinstance(value, str):
                last_verified_at = datetime.fromisoformat(value)
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            pass
        return BrowserStatus(self.profile.exists(), last_verified_at)

    def record_verification(self) -> None:
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text(
            json.dumps({"last_verified_at": datetime.now(UTC).isoformat()}), encoding="utf-8"
        )

    def clear(self) -> None:
        with ProfileLock(self.profile):
            if self.profile.exists():
                shutil.rmtree(self.profile)
            self.state.unlink(missing_ok=True)
