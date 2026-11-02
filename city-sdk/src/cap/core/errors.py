"""City error codes and the CAP error body (``specs/cap/error.json``)."""

from __future__ import annotations

# specification 04 §6.9. Profile codes are listed there and are not in error-codes.json.
HTTP_STATUS: dict[str, int] = {
    "SCHEMA_INVALID": 400,
    "SIGNATURE_INVALID": 401,
    "KEY_UNKNOWN": 401,
    "GRANT_EXPIRED": 401,
    "GRANT_PERMISSION_DENIED": 403,
    "TARGET_NOT_VISIBLE": 404,
    "IDEMPOTENCY_CONFLICT": 400,
    "DUPLICATE": 409,
    "EXPIRED": 410,
    "POLICY_VIOLATION": 422,
    "STALE_FENCE": 422,
    "STALE_EPOCH": 422,
    "OUT_OF_SCOPE": 422,
    "NO_CAPACITY": 422,
    "CONSENT_MISSING": 422,
    "RATE_LIMITED": 429,
    "DRAINING": 503,
    "QUARANTINED": 503,
    "SUSPENDED": 503,
    "UPSTREAM_TIMEOUT": 504,
    "AGENT_LOST_CUSTODY": 502,
}


class CapError(Exception):
    """A CAP failure. ``body()`` matches ``specs/cap/error.json``."""

    def __init__(self, code: str, message: str = "", msg_id: str | None = None) -> None:
        self.code = code
        self.message = message
        self.msg_id = msg_id
        super().__init__(code if not message else f"{code}: {message}")

    def http_status(self) -> int:
        try:
            return HTTP_STATUS[self.code]
        except KeyError as exc:
            raise CapError("SCHEMA_INVALID", f"unknown error code {self.code}") from exc

    def body(self) -> dict[str, str]:
        payload: dict[str, str] = {"code": self.code}
        if self.message:
            payload["message"] = self.message
        if self.msg_id is not None:
            payload["msg_id"] = self.msg_id
        return payload


def http_status_for(code: str) -> int:
    try:
        return HTTP_STATUS[code]
    except KeyError as exc:
        raise CapError("SCHEMA_INVALID", f"unknown error code {code}") from exc
