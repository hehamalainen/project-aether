"""OtterBot runtime exceptions."""

from __future__ import annotations


class OtterBotError(Exception):
    """Base exception for OtterBot."""


class AuthError(OtterBotError):
    """Authentication or token validation failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class PolicyError(OtterBotError):
    """Policy evaluation failure."""


class SkillAdmissionError(OtterBotError):
    """Raised when skill package fails supply-chain checks."""


class ToolExecutionError(OtterBotError):
    """Raised for tool execution failures."""


class QuotaExceededError(ToolExecutionError):
    """Raised when per-session execution quota is exceeded."""


class MemoryAccessDenied(OtterBotError):
    """Raised when session memory isolation boundary is violated."""
