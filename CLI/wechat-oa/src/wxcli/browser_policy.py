"""Durable browser-fallback authorization and per-invocation resolution."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import uuid4

from wxcli.errors import ErrorCode, InputError, WxcliError


class BrowserFallbackPolicy(StrEnum):
    NEVER = "never"
    AUTO_FALLBACK = "auto-fallback"


class BrowserMode(StrEnum):
    NEVER = "never"
    DIRECT = "direct"
    AUTO_FALLBACK = "auto-fallback"


class BrowserPolicySource(StrEnum):
    DEFAULT = "default"
    USER_CONFIG = "user_config"
    REQUEST_JSON = "request_json"
    CLI = "cli"
    INVALID_POLICY = "invalid_policy"


@dataclass(frozen=True, slots=True)
class BrowserPolicyStatus:
    policy: BrowserFallbackPolicy
    configured: bool
    valid: bool
    warning: Literal["browser_policy_invalid"] | None = None


@dataclass(frozen=True, slots=True)
class BrowserDecision:
    mode: BrowserMode
    source: BrowserPolicySource
    warning: Literal["browser_policy_invalid"] | None = None

    @property
    def allows_fallback(self) -> bool:
        return self.mode is BrowserMode.AUTO_FALLBACK


class BrowserPolicyStore:
    """Own non-secret user policy with fail-closed reads and atomic writes."""

    SCHEMA_VERSION = "1"

    def __init__(self, path: Path) -> None:
        self.path = path

    def status(self, *, strict: bool = False) -> BrowserPolicyStatus:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or set(raw) != {"schema_version", "policy"}:
                raise ValueError("invalid browser policy object")
            if raw["schema_version"] != self.SCHEMA_VERSION:
                raise ValueError("unsupported browser policy schema")
            policy = BrowserFallbackPolicy(raw["policy"])
            return BrowserPolicyStatus(policy=policy, configured=True, valid=True)
        except FileNotFoundError:
            return BrowserPolicyStatus(
                policy=BrowserFallbackPolicy.NEVER,
                configured=False,
                valid=True,
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            if strict:
                raise WxcliError(
                    ErrorCode.LOCAL_CONFIGURATION_ERROR,
                    "The browser fallback policy is invalid.",
                    {"warning": "browser_policy_invalid"},
                ) from error
            return BrowserPolicyStatus(
                policy=BrowserFallbackPolicy.NEVER,
                configured=True,
                valid=False,
                warning="browser_policy_invalid",
            )

    def set(self, policy: BrowserFallbackPolicy) -> BrowserPolicyStatus:
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        payload = {"schema_version": self.SCHEMA_VERSION, "policy": policy.value}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except OSError as error:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise WxcliError(
                ErrorCode.LOCAL_CONFIGURATION_ERROR,
                "The browser fallback policy could not be saved.",
            ) from error
        return BrowserPolicyStatus(policy=policy, configured=True, valid=True)


def resolve_browser_decision(
    store: BrowserPolicyStore,
    *,
    browser: bool = False,
    browser_fallback: bool = False,
    no_browser: bool = False,
    request_allow_browser: bool = False,
    browser_is_direct: bool = False,
) -> BrowserDecision:
    """Resolve invocation controls before any source URL is opened."""
    if sum((browser, browser_fallback, no_browser)) > 1:
        raise InputError("Browser controls cannot be combined.")
    if no_browser:
        return BrowserDecision(BrowserMode.NEVER, BrowserPolicySource.CLI)
    if browser:
        mode = BrowserMode.DIRECT if browser_is_direct else BrowserMode.AUTO_FALLBACK
        return BrowserDecision(mode, BrowserPolicySource.CLI)
    if browser_fallback:
        return BrowserDecision(BrowserMode.AUTO_FALLBACK, BrowserPolicySource.CLI)
    if request_allow_browser:
        return BrowserDecision(BrowserMode.AUTO_FALLBACK, BrowserPolicySource.REQUEST_JSON)

    status = store.status()
    if not status.valid:
        return BrowserDecision(
            BrowserMode.NEVER,
            BrowserPolicySource.INVALID_POLICY,
            warning=status.warning,
        )
    mode = (
        BrowserMode.AUTO_FALLBACK
        if status.policy is BrowserFallbackPolicy.AUTO_FALLBACK
        else BrowserMode.NEVER
    )
    source = BrowserPolicySource.USER_CONFIG if status.configured else BrowserPolicySource.DEFAULT
    return BrowserDecision(mode, source)
