"""Tests for offline-by-default doctor diagnostics."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from wxcli.auth import AccessToken, AccessTokenStore, AppIdStore, SecretStore
from wxcli.browser import BrowserProfile, ProfileLock
from wxcli.doctor import Doctor
from wxcli.errors import ErrorCode, WxcliError


class FakeBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.values.pop((service_name, username), None)


def configured_doctor(tmp_path: Path) -> tuple[Doctor, BrowserProfile]:
    backend = FakeBackend()
    appids = AppIdStore(tmp_path / "runtime" / "config.json")
    appids.put("fake-appid")
    secrets = SecretStore(backend)
    secrets.set_app_secret("fake-secret")
    tokens = AccessTokenStore(backend, tmp_path / "runtime" / "token-state.json")
    tokens.put(AccessToken("fake-token", datetime.now(UTC) + timedelta(hours=1)))
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"stub")
    profile = BrowserProfile(tmp_path / "profile", tmp_path / "browser-state.json")
    return (
        Doctor(
            runtime_root=tmp_path / "runtime",
            chrome_path=chrome,
            browser_profile=profile,
            appids=appids,
            secrets=secrets,
            tokens=tokens,
        ),
        profile,
    )


def test_doctor_skips_network_and_account_checks_by_default(tmp_path: Path) -> None:
    doctor, _ = configured_doctor(tmp_path)

    report = doctor.run()
    statuses = {check.name: check.status for check in report.checks}

    assert report.overall == "pass"
    assert statuses["chrome"] == "pass"
    assert statuses["runtime_directory"] == "pass"
    assert statuses["credentials"] == "pass"
    assert statuses["token_cache"] == "pass"
    assert statuses["network"] == "skip"
    assert statuses["stable_token"] == "skip"
    assert statuses["draft_permission"] == "skip"


def test_doctor_warns_when_profile_lock_is_held(tmp_path: Path) -> None:
    doctor, profile = configured_doctor(tmp_path)

    with ProfileLock(profile.profile):
        report = doctor.run()

    lock_check = next(check for check in report.checks if check.name == "profile_lock")
    assert lock_check.status == "warn"
    assert report.overall == "pass"


def test_doctor_fails_when_profile_lock_cannot_be_created(tmp_path: Path) -> None:
    doctor, profile = configured_doctor(tmp_path)
    blocked_parent = tmp_path / "blocked"
    blocked_parent.write_text("not a directory", encoding="utf-8")
    profile.profile = blocked_parent / "profile"

    report = doctor.run()

    lock_check = next(check for check in report.checks if check.name == "profile_lock")
    assert lock_check.status == "fail"
    assert report.overall == "fail"


def test_doctor_fails_when_required_local_configuration_is_missing(tmp_path: Path) -> None:
    backend = FakeBackend()
    profile = BrowserProfile(tmp_path / "profile", tmp_path / "state.json")
    doctor = Doctor(
        runtime_root=tmp_path / "runtime",
        chrome_path=tmp_path / "missing-chrome.exe",
        browser_profile=profile,
        appids=AppIdStore(tmp_path / "runtime" / "config.json"),
        secrets=SecretStore(backend),
        tokens=AccessTokenStore(backend, tmp_path / "runtime" / "token-state.json"),
    )

    report = doctor.run()
    statuses = {check.name: check.status for check in report.checks}

    assert report.overall == "fail"
    assert statuses["chrome"] == "fail"
    assert statuses["credentials"] == "fail"
    assert statuses["token_cache"] == "warn"


def test_live_error_summary_never_copies_api_message() -> None:
    checks = Doctor._live_error_checks(
        WxcliError(
            ErrorCode.AUTHENTICATION_ERROR,
            "safe message",
            {
                "errcode": 48001,
                "checks": {
                    "stable_token": "pass",
                    "ip_allowlist": "pass",
                    "draft_batchget": "pass",
                    "freepublish_batchget": "fail",
                },
            },
        )
    )

    assert {check.name: check.status for check in checks} == {
        "stable_token": "pass",
        "ip_allowlist": "pass",
        "draft_permission": "pass",
        "published_permission": "fail",
    }
    assert all(check.message in {"Read-only check passed.", "Read-only check failed."} for check in checks)
