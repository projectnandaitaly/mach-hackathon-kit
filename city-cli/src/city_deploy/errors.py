"""Deploy-local failures. Quota is not a city-wide error code."""

from __future__ import annotations


class QuotaExceeded(Exception):
    def __init__(self, message: str = "") -> None:
        self.message = message
        super().__init__(message)

    def body(self) -> dict[str, str]:
        payload = {"code": "QUOTA_EXCEEDED"}
        if self.message:
            payload["message"] = self.message
        return payload


class Conflict(Exception):
    """HTTP 409. The Index visibility machine is not extended here."""

    def __init__(self, message: str = "") -> None:
        self.message = message
        super().__init__(message)

    def body(self) -> dict[str, str]:
        payload = {"code": "POLICY_VIOLATION"}
        if self.message:
            payload["message"] = self.message
        return payload
