"""Visibility state machine (ADR-074). An agent is ready, or the prober is holding it."""

from __future__ import annotations

from cap.core.errors import CapError

Visibility = str

STATES: frozenset[str] = frozenset({"visible", "degraded", "unreachable"})

# The prober walks both ways. The team role may only return an agent to visible.
ALLOWED: frozenset[tuple[str, str]] = frozenset(
    {
        ("visible", "degraded"),
        ("degraded", "visible"),
        ("visible", "unreachable"),
        ("unreachable", "visible"),
    }
)

# Discovery, the map, and the scoreboard. A hold is not ready, so it is absent.
DEFAULT_QUERY: frozenset[str] = frozenset({"visible"})
TEAM_RESTORE: frozenset[str] = frozenset({"degraded", "unreachable"})


def assert_transition(current: str, nxt: str) -> None:
    if current not in STATES or nxt not in STATES:
        raise CapError("SCHEMA_INVALID", f"unknown visibility {current!r} -> {nxt!r}")
    if (current, nxt) not in ALLOWED:
        raise CapError("POLICY_VIOLATION", f"disallowed transition {current} -> {nxt}")
