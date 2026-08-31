"""Tests for durable browser policy and invocation precedence."""

from __future__ import annotations

from pathlib import Path

import pytest

import wxcli.browser_policy as policy_module
from wxcli.browser_policy import (
    BrowserFallbackPolicy,
    BrowserMode,
    BrowserPolicySource,
    BrowserPolicyStore,
    resolve_browser_decision,
)
from wxcli.errors import ErrorCode, WxcliError


def test_missing_policy_fails_closed_and_set_is_atomic(tmp_path: Path) -> None:
    store = BrowserPolicyStore(tmp_path / "browser-policy.json")
    missing = store.status()
    assert missing.policy is BrowserFallbackPolicy.NEVER
    assert missing.configured is False
    assert missing.valid is True

    saved = store.set(BrowserFallbackPolicy.AUTO_FALLBACK)
    assert saved.policy is BrowserFallbackPolicy.AUTO_FALLBACK
    assert store.status().policy is BrowserFallbackPolicy.AUTO_FALLBACK
    assert list(tmp_path.glob("*.tmp")) == []


def test_invalid_policy_is_safe_for_runtime_but_status_is_nonzero_error(tmp_path: Path) -> None:
    path = tmp_path / "browser-policy.json"
    path.write_text('{"schema_version":"999","policy":"auto-fallback"}', encoding="utf-8")
    store = BrowserPolicyStore(path)

    runtime = store.status()
    assert runtime.policy is BrowserFallbackPolicy.NEVER
    assert runtime.valid is False
    assert runtime.warning == "browser_policy_invalid"

    with pytest.raises(WxcliError) as raised:
        store.status(strict=True)
    assert raised.value.code is ErrorCode.LOCAL_CONFIGURATION_ERROR


def test_invocation_precedence_and_request_grant(tmp_path: Path) -> None:
    store = BrowserPolicyStore(tmp_path / "browser-policy.json")
    store.set(BrowserFallbackPolicy.AUTO_FALLBACK)

    assert resolve_browser_decision(store).source is BrowserPolicySource.USER_CONFIG
    assert resolve_browser_decision(store).mode is BrowserMode.AUTO_FALLBACK
    prohibited = resolve_browser_decision(store, no_browser=True, request_allow_browser=True)
    assert prohibited.mode is BrowserMode.NEVER
    assert prohibited.source is BrowserPolicySource.CLI
    request = resolve_browser_decision(store, request_allow_browser=True)
    assert request.source is BrowserPolicySource.REQUEST_JSON
    direct = resolve_browser_decision(store, browser=True, browser_is_direct=True)
    assert direct.mode is BrowserMode.DIRECT


def test_explicit_controls_work_with_invalid_policy_and_conflicts_fail(tmp_path: Path) -> None:
    path = tmp_path / "browser-policy.json"
    path.write_text("not-json", encoding="utf-8")
    store = BrowserPolicyStore(path)

    fallback = resolve_browser_decision(store, browser_fallback=True)
    assert fallback.mode is BrowserMode.AUTO_FALLBACK
    assert fallback.warning is None
    invalid = resolve_browser_decision(store)
    assert invalid.mode is BrowserMode.NEVER
    assert invalid.source is BrowserPolicySource.INVALID_POLICY

    with pytest.raises(WxcliError) as raised:
        resolve_browser_decision(store, browser=True, no_browser=True)
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT


def test_policy_rejects_extra_fields_and_write_failure_cleans_temporary_file(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "browser-policy.json"
    path.write_text(
        '{"schema_version":"1","policy":"never","extra":true}',
        encoding="utf-8",
    )
    assert BrowserPolicyStore(path).status().valid is False

    path.unlink()
    monkeypatch.setattr(
        policy_module.os,
        "replace",
        lambda *args: (_ for _ in ()).throw(OSError("disk")),
    )
    with pytest.raises(WxcliError) as raised:
        BrowserPolicyStore(path).set(BrowserFallbackPolicy.AUTO_FALLBACK)
    assert raised.value.code is ErrorCode.LOCAL_CONFIGURATION_ERROR
    assert list(tmp_path.glob("*.tmp")) == []
