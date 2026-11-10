"""ADR-109 T13–T14: what Deploy instances agree on while pipelines run on any of them.

``SharedRollouts`` is the T3 admission queue held in ``deployments`` rows: one arrival
order and the same caps for every team, counted across instances. ``SharedLocks`` keeps
one writer per team for purge and delete, while rollouts of that team share it.
Nothing here ranks or chooses: order is arrival, and every team has the same cap.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from city_deploy.clock import Clock
from city_deploy.errors import Conflict
from city_deploy.rollouts import RolloutGate
from city_deploy.shared import LEASE, DeployRows, SharedDeployments


class NoLocks:
    """One instance: the snapshot store keeps today's behaviour and takes no lock."""

    @asynccontextmanager
    async def hold(
        self, name: str, *, exclusive: bool, wait: float | None = None
    ) -> AsyncIterator[None]:
        del name, exclusive, wait
        yield

    def held(self) -> list[tuple[str, str]]:
        return []

    def renew(self, held: list[tuple[str, str]]) -> None:
        del held


class SharedLocks:
    """Named reader/writer locks in ``deploy_locks``. Rows expire unless renewed."""

    def __init__(self, rows: DeployRows, clock: Clock, instance: str, *, poll: float = 0.5):
        self._rows = rows
        self._clock = clock
        self._instance = instance
        self.poll = poll
        self._held: set[tuple[str, str]] = set()

    @asynccontextmanager
    async def hold(
        self, name: str, *, exclusive: bool, wait: float | None = None
    ) -> AsyncIterator[None]:
        """Hold ``name``. ``wait`` seconds without it raise Conflict; None waits on."""

        holder = f"{self._instance}:{uuid.uuid4().hex[:8]}"
        loop = asyncio.get_running_loop()
        deadline = None if wait is None else loop.time() + wait
        key = (name, holder)
        # A waiting writer keeps its place by asking again; renew keeps it too.
        self._held.add(key)
        try:
            while not await asyncio.to_thread(self._ask, name, holder, exclusive):
                if deadline is not None and loop.time() >= deadline:
                    raise Conflict(f"{name} is busy; try again")
                await asyncio.sleep(self.poll)
            yield
        finally:
            self._held.discard(key)
            await asyncio.to_thread(self._rows.unlock, name, holder)

    def _ask(self, name: str, holder: str, exclusive: bool) -> bool:
        now = self._clock.now()
        return self._rows.lock(name, holder, exclusive=exclusive, now=now, until=now + LEASE)

    def held(self) -> list[tuple[str, str]]:
        return sorted(self._held)

    def renew(self, held: list[tuple[str, str]]) -> None:
        until = self._clock.now() + LEASE
        for name, holder in held:
            self._rows.renew_lock(name, holder, until)


class SharedRollouts:
    """``RolloutGate`` across instances: admission is one transaction on the rows."""

    def __init__(
        self,
        rows: DeployRows,
        deployments: SharedDeployments,
        clock: Clock,
        gate: RolloutGate,
        *,
        poll: float = 1.0,
    ) -> None:
        self._rows = rows
        self._deployments = deployments
        self._clock = clock
        self.limit = gate.limit
        self.per_team = gate.per_team
        self.poll = poll

    @asynccontextmanager
    async def slot(
        self, deployment_id: str, on_position: Callable[[int], None] = lambda _ahead: None
    ) -> AsyncIterator[None]:
        fence = self._deployments.fence(deployment_id)
        try:
            while True:
                ahead = await asyncio.to_thread(self._admit, deployment_id, fence)
                if ahead is None:
                    break
                on_position(ahead)
                await asyncio.sleep(self.poll)
            yield
        finally:
            await asyncio.to_thread(self._rows.leave, deployment_id, fence)

    def _admit(self, deployment_id: str, fence: int) -> int | None:
        return self._rows.admit(
            deployment_id,
            fence,
            limit=self.limit,
            per_team=self.per_team,
            now=self._clock.now(),
        )
