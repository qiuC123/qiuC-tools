"""Offline-by-default environment diagnostics for wxcli."""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict

from wxcli.auth import AccessTokenStore, AppIdStore, SecretStore, TokenManager
from wxcli.browser import BrowserProfile, ProfileLock
from wxcli.errors import WxcliError
from wxcli.official_check import OfficialReadOnlyChecker

DoctorStatus = Literal["pass", "fail", "skip", "warn"]


class DoctorCheck(BaseModel):
    """One safe diagnostic result."""

    model_config = ConfigDict(extra="forbid")

    name: str
    status: DoctorStatus
    message: str


class DoctorReport(BaseModel):
    """A complete diagnostic report with no credential values."""

    model_config = ConfigDict(extra="forbid")

    overall: Literal["pass", "fail"]
    checks: list[DoctorCheck]


class Doctor:
    """Check local prerequisites and optionally authorized live API access."""

    def __init__(
        self,
        *,
        runtime_root: Path,
        chrome_path: Path,
        browser_profile: BrowserProfile,
        appids: AppIdStore,
        secrets: SecretStore,
        tokens: AccessTokenStore,
    ) -> None:
        self._runtime_root = runtime_root
        self._chrome_path = chrome_path
        self._browser_profile = browser_profile
        self._appids = appids
        self._secrets = secrets
        self._tokens = tokens

    def run(self, *, allow_live_api: bool = False) -> DoctorReport:
        checks = [
            self._chrome(),
            self._writable_directory(),
            self._profile_lock(),
        ]
        appid = self._appids.get()
        try:
            has_secret = self._secrets.get_app_secret() is not None
        except WxcliError:
            has_secret = False
        checks.append(
            DoctorCheck(
                name="credentials",
                status="pass" if appid and has_secret else "fail",
                message=(
                    "AppID and AppSecret are configured."
                    if appid and has_secret
                    else "AppID or AppSecret is not configured."
                ),
            )
        )
        cached = self._tokens.get(datetime.now(UTC))
        checks.append(
            DoctorCheck(
                name="token_cache",
                status="pass" if cached else "warn",
                message="A non-stale token is cached." if cached else "No non-stale token is cached.",
            )
        )

        if not allow_live_api:
            checks.append(DoctorCheck(name="network", status="skip", message="Live checks were not authorized."))
            checks.extend(self._skipped_live_checks())
        elif not appid or not has_secret:
            checks.append(DoctorCheck(name="network", status="skip", message="Credentials are incomplete."))
            checks.extend(self._skipped_live_checks("Credentials are incomplete."))
        else:
            checks.extend(self._live_checks(appid))

        overall: Literal["pass", "fail"] = (
            "fail" if any(check.status == "fail" for check in checks) else "pass"
        )
        return DoctorReport(overall=overall, checks=checks)

    def _chrome(self) -> DoctorCheck:
        exists = self._chrome_path.is_file()
        return DoctorCheck(
            name="chrome",
            status="pass" if exists else "fail",
            message="Google Chrome was found." if exists else "Google Chrome was not found.",
        )

    def _writable_directory(self) -> DoctorCheck:
        try:
            self._runtime_root.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self._runtime_root, prefix="doctor-", delete=True):
                pass
        except OSError:
            return DoctorCheck(
                name="runtime_directory",
                status="fail",
                message="The wxcli runtime directory is not writable.",
            )
        return DoctorCheck(
            name="runtime_directory",
            status="pass",
            message="The wxcli runtime directory is writable.",
        )

    def _profile_lock(self) -> DoctorCheck:
        try:
            with ProfileLock(self._browser_profile.profile):
                pass
        except WxcliError:
            return DoctorCheck(
                name="profile_lock",
                status="warn",
                message="The wxcli Chrome profile is currently locked.",
            )
        except OSError:
            return DoctorCheck(
                name="profile_lock",
                status="fail",
                message="The wxcli Chrome profile lock cannot be created.",
            )
        return DoctorCheck(
            name="profile_lock",
            status="pass",
            message="The wxcli Chrome profile lock is available.",
        )

    def _live_checks(self, appid: str) -> list[DoctorCheck]:
        try:
            with httpx.Client(timeout=10.0) as client:
                client.get("https://api.weixin.qq.com")
                network = DoctorCheck(
                    name="network",
                    status="pass",
                    message="The WeChat API host is reachable.",
                )
                manager = TokenManager(client, appid, self._secrets, self._tokens)
                try:
                    OfficialReadOnlyChecker(client, manager).run()
                except WxcliError as error:
                    return [network, *self._live_error_checks(error)]
        except httpx.HTTPError:
            return [
                DoctorCheck(
                    name="network",
                    status="fail",
                    message="The WeChat API host could not be reached.",
                ),
                *self._skipped_live_checks("Network check failed."),
            ]
        return [
            network,
            DoctorCheck(name="stable_token", status="pass", message="Stable token check passed."),
            DoctorCheck(name="ip_allowlist", status="pass", message="IP allowlist check passed."),
            DoctorCheck(name="draft_permission", status="pass", message="Draft list permission passed."),
            DoctorCheck(
                name="published_permission",
                status="pass",
                message="Published list permission passed.",
            ),
        ]

    @staticmethod
    def _live_error_checks(error: WxcliError) -> list[DoctorCheck]:
        reported = error.details.get("checks")
        states = reported if isinstance(reported, dict) else {}
        mapping = [
            ("stable_token", "stable_token"),
            ("ip_allowlist", "ip_allowlist"),
            ("draft_permission", "draft_batchget"),
            ("published_permission", "freepublish_batchget"),
        ]
        results: list[DoctorCheck] = []
        for name, source in mapping:
            state = states.get(source)
            status: DoctorStatus
            if state == "pass":
                status = "pass"
            elif state == "fail":
                status = "fail"
            else:
                status = "skip"
            results.append(
                DoctorCheck(
                    name=name,
                    status=status,
                    message=(
                        "Read-only check passed."
                        if status == "pass"
                        else "Read-only check failed."
                        if status == "fail"
                        else "Read-only check was not reached."
                    ),
                )
            )
        return results

    @staticmethod
    def _skipped_live_checks(message: str = "Live checks were not authorized.") -> list[DoctorCheck]:
        return [
            DoctorCheck(name=name, status="skip", message=message)
            for name in ("stable_token", "ip_allowlist", "draft_permission", "published_permission")
        ]
