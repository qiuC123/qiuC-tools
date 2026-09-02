from __future__ import annotations


class CLIError(RuntimeError):
    """Expected, actionable CLI error."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}
