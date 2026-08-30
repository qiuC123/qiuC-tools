"""Stable error codes and process exit codes for wxcli."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any


class ExitCode(IntEnum):
    """The public process exit-code contract."""

    SUCCESS = 0
    GENERAL = 1
    INPUT = 2
    VALIDATION = 3
    NOT_FOUND = 4
    NETWORK = 5
    AUTHENTICATION = 6
    CHROME = 7
    PARSING = 8
    LOCAL_CONFIGURATION = 9


class ErrorCode(StrEnum):
    """Machine-readable error identifiers safe to present to callers."""

    GENERAL_ERROR = "GENERAL_ERROR"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    NETWORK_ERROR = "NETWORK_ERROR"
    AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
    CHROME_ERROR = "CHROME_ERROR"
    BROWSER_BUSY = "BROWSER_BUSY"
    PARSING_ERROR = "PARSING_ERROR"
    LOCAL_CONFIGURATION_ERROR = "LOCAL_CONFIGURATION_ERROR"
    VERIFICATION_REQUIRED = "VERIFICATION_REQUIRED"


ERROR_EXIT_CODES: dict[ErrorCode, ExitCode] = {
    ErrorCode.GENERAL_ERROR: ExitCode.GENERAL,
    ErrorCode.INVALID_ARGUMENT: ExitCode.INPUT,
    ErrorCode.VALIDATION_ERROR: ExitCode.VALIDATION,
    ErrorCode.NOT_FOUND: ExitCode.NOT_FOUND,
    ErrorCode.NETWORK_ERROR: ExitCode.NETWORK,
    ErrorCode.AUTHENTICATION_ERROR: ExitCode.AUTHENTICATION,
    ErrorCode.CHROME_ERROR: ExitCode.CHROME,
    ErrorCode.BROWSER_BUSY: ExitCode.CHROME,
    ErrorCode.PARSING_ERROR: ExitCode.PARSING,
    ErrorCode.LOCAL_CONFIGURATION_ERROR: ExitCode.LOCAL_CONFIGURATION,
    ErrorCode.VERIFICATION_REQUIRED: ExitCode.AUTHENTICATION,
}


@dataclass(slots=True)
class WxcliError(Exception):
    """An expected, safe-to-display application error."""

    code: ErrorCode
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    exit_code: ExitCode = field(init=False)

    def __post_init__(self) -> None:
        self.exit_code = ERROR_EXIT_CODES[self.code]

    def __str__(self) -> str:
        return self.message


class InputError(WxcliError):
    """Invalid command input."""

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(ErrorCode.INVALID_ARGUMENT, message, details)


class ValidationError(WxcliError):
    """Input that is structurally valid but violates a product rule."""

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(ErrorCode.VALIDATION_ERROR, message, details)


class NotFoundError(WxcliError):
    """A requested resource does not exist."""

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(ErrorCode.NOT_FOUND, message, details)


class VerificationRequiredError(WxcliError):
    """Human browser verification is required before continuing."""

    def __init__(
        self,
        message: str = "WeChat requires browser verification.",
        **details: Any,
    ) -> None:
        super().__init__(
            ErrorCode.VERIFICATION_REQUIRED,
            message,
            details,
        )
