"""Catalog records (Firestore-shaped, in memory for CI)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentRecord:
    agent_id: str
    team_id: str
    visibility: str
    card: dict[str, Any]
    card_digest: str
    active_version: str
    rating_count: int = 0
    role: str | None = None
    capabilities: list[str] = field(default_factory=list)
    districts: list[int] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    accessibility: list[str] = field(default_factory=list)
    profiles: list[str] = field(default_factory=list)
    keys: dict[str, dict[str, str]] = field(default_factory=dict)
    quarantine_until: str | None = None
    created_at: str | None = None
    promoted_at: str | None = None
    # City impostors. Absent from the default query; returned with include=unverified.
    unverified: bool = False


@dataclass(frozen=True)
class Placement:
    """City-owned invoke address. It is not a field of the agent card."""

    invoke_base_url: str
    revision_tag: str


@dataclass(frozen=True)
class VisibilityLogEntry:
    agent_id: str
    from_state: str
    to_state: str
    cause: str
    observer: str
    at: str
