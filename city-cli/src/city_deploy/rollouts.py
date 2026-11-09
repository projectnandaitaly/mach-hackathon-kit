"""Admission to the cluster-heavy phases of a release: rollout, conformance, promote.

Strict arrival order with the same cap for every team. A waiting release whose team is
at its cap does not hold back a later release of another team. Nothing is ranked.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

DEFAULT_LIMIT = 10
DEFAULT_PER_TEAM = 2


@dataclass(eq=False)
class _Ticket:
    team_id: str
    on_position: Callable[[int], None]
    admitted: asyncio.Event = field(default_factory=asyncio.Event)


class RolloutGate:
    def __init__(self, limit: int = DEFAULT_LIMIT, per_team: int = DEFAULT_PER_TEAM) -> None:
        if limit < 1 or per_team < 1:
            raise ValueError("rollout limits must be positive")
        self.limit = limit
        self.per_team = per_team
        self._active: dict[str, int] = {}
        self._waiting: list[_Ticket] = []

    @classmethod
    def from_env(cls) -> RolloutGate:
        limit = int(os.environ.get("CITY_DEPLOY_ROLLOUTS", str(DEFAULT_LIMIT)))
        per_team = int(os.environ.get("CITY_DEPLOY_TEAM_ROLLOUTS", str(DEFAULT_PER_TEAM)))
        return cls(limit, per_team)

    @property
    def active(self) -> int:
        return sum(self._active.values())

    def active_for(self, team_id: str) -> int:
        return self._active.get(team_id, 0)

    @property
    def waiting(self) -> int:
        return len(self._waiting)

    @asynccontextmanager
    async def slot(
        self, team_id: str, on_position: Callable[[int], None] = lambda _ahead: None
    ) -> AsyncIterator[None]:
        """Hold one slot for the body. ``on_position`` gets the releases ahead while waiting."""

        ticket = _Ticket(team_id, on_position)
        self._waiting.append(ticket)
        self._admit()
        try:
            await ticket.admitted.wait()
        except BaseException:
            if ticket in self._waiting:
                self._waiting.remove(ticket)
                self._admit()
            else:
                self._release(team_id)
            raise
        try:
            yield
        finally:
            self._release(team_id)

    def _release(self, team_id: str) -> None:
        left = self._active.get(team_id, 0) - 1
        if left > 0:
            self._active[team_id] = left
        else:
            self._active.pop(team_id, None)
        self._admit()

    def _admit(self) -> None:
        for ticket in list(self._waiting):
            if self.active >= self.limit:
                break
            if self.active_for(ticket.team_id) >= self.per_team:
                continue
            self._waiting.remove(ticket)
            self._active[ticket.team_id] = self.active_for(ticket.team_id) + 1
            ticket.admitted.set()
        for ahead, ticket in enumerate(self._waiting):
            ticket.on_position(ahead)
