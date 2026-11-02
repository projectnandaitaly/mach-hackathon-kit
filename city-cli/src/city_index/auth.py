"""Caller role stubs for Deploy / Governance / Prober."""

from __future__ import annotations

from cap.core.errors import CapError

DEPLOY = "deploy"
GOVERNANCE = "governance"
PROBER = "prober"
TEAM = "team"


def require_role(header: str | None, *allowed: str) -> str:
    if header is None or header not in allowed:
        raise CapError("POLICY_VIOLATION", "caller not authorized")
    return header
