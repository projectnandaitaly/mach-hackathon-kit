"""Role query, one agent per team, and the deterministic shuffle (ADR-004, ADR-085)."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable
from typing import Any

from city_index.families import card_capabilities, family_of
from city_index.model import AgentRecord
from city_index.visibility import DEFAULT_QUERY


def matches(agent: AgentRecord, *, role: str, include_unverified: bool) -> bool:
    if agent.unverified and not include_unverified:
        return False
    if not agent.unverified and agent.visibility not in DEFAULT_QUERY:
        return False
    return family_of(agent.card) == role


def latest_per_team(agents: Iterable[AgentRecord]) -> list[AgentRecord]:
    """The last promoted agent of each team. The input is already one family."""

    latest: dict[str, AgentRecord] = {}
    for agent in agents:
        current = latest.get(agent.team_id)
        if current is None or _promoted(agent) > _promoted(current):
            latest[agent.team_id] = agent
    return list(latest.values())


def _promoted(agent: AgentRecord) -> tuple[str, str]:
    return (agent.promoted_at or agent.created_at or "", agent.agent_id)


def shuffle(
    agents: list[AgentRecord],
    *,
    caller: str,
    minute_bucket: str,
) -> list[AgentRecord]:
    """Order by agent_id under a deterministic per-requester permutation."""
    seed = hashlib.sha256(f"{caller}|{minute_bucket}".encode()).digest()

    def key(agent: AgentRecord) -> bytes:
        return hmac.new(seed, agent.agent_id.encode(), hashlib.sha256).digest()

    return sorted(agents, key=key)


def summary(agent: AgentRecord, *, role: str, reputation: float) -> dict[str, Any]:
    """``AgentSummary``: the id, the card fields a caller picks by, and the reputation."""

    card = agent.card
    return {
        "agent_id": agent.agent_id,
        "profiles": _strings(card.get("profiles")),
        "role": role,
        "capabilities": card_capabilities(card),
        "no_clinical_advice": card.get("no_clinical_advice") is True,
        "reputation": reputation,
    }


def _strings(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def cache_key(*, role: str, include_unverified: bool, caller: str, minute_bucket: str) -> str:
    return "|".join([role, "1" if include_unverified else "0", caller, minute_bucket])
