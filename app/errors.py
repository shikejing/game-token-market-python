"""Domain error types."""
from __future__ import annotations


class MarketError(Exception):
    """Base error for the market service."""


class Rejected(MarketError):
    """Order request rejected (no state change)."""

    def __init__(self, reason: str, message: str = ""):
        self.reason = reason
        super().__init__(message or reason)


class NotFound(MarketError):
    """Referenced resource does not exist."""


class Conflict(MarketError):
    """Request conflicts with current state (e.g. cancel of non-open order)."""


class StorageError(MarketError):
    """Durable persistence failed."""
