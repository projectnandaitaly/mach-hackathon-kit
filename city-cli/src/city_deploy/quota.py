"""Team quotas and the per-agent scale caps from specification 10 §10.5."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from cap.core.errors import CapError

from city_deploy.errors import QuotaExceeded

# Temporary hackathon headroom (was 5 agents / 10 deploys/hour). Tighten before freeze.
MAX_AGENTS = 1000
MAX_DEPLOYS_PER_HOUR = 1000
MAX_CONCURRENT_BUILDS = 3
MAX_REPLICAS = 10
MAX_CONCURRENCY = 80
MIN_REPLICAS = 1
MAX_IMAGE_BYTES = 2 * 1024 * 1024 * 1024


def check_scale(*, min_replicas: int, max_replicas: int, concurrency: int) -> None:
    if min_replicas < MIN_REPLICAS or min_replicas > max_replicas:
        raise CapError("POLICY_VIOLATION", "replicas")
    if max_replicas > MAX_REPLICAS:
        raise CapError("POLICY_VIOLATION", "max replicas")
    if not 1 <= concurrency <= MAX_CONCURRENCY:
        raise CapError("POLICY_VIOLATION", "concurrency")


def recent(moments: list[datetime], now: datetime) -> list[datetime]:
    window = now - timedelta(hours=1)
    return [moment for moment in moments if moment > window]


def assert_create(
    *,
    known_agents: set[str],
    agent_id: str,
    recent_deploys: int,
) -> None:
    if agent_id not in known_agents and len(known_agents) >= MAX_AGENTS:
        raise QuotaExceeded("agents")
    if recent_deploys >= MAX_DEPLOYS_PER_HOUR:
        raise QuotaExceeded("deploys")


def assert_build(*, building: int) -> None:
    if building >= MAX_CONCURRENT_BUILDS:
        raise QuotaExceeded("builds")


class UpdateInterval(Protocol):
    """Minutes City Hall stored. Deploy only reads them (ADR-120)."""

    def minutes(self) -> int: ...


def assert_agent_update(*, previous: datetime | None, now: datetime, minutes: int) -> None:
    """Refuse a second accept of the same agent before ``minutes`` have passed.

    ``minutes`` of 0 is off. A missing ``previous`` is the agent's first accept.
    """

    if minutes <= 0 or previous is None:
        return
    if now < previous + timedelta(minutes=minutes):
        raise QuotaExceeded(f"this agent can be updated once every {minutes} minutes")
