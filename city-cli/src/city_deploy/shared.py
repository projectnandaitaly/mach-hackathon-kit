"""ADR-109: Deploy state as SQL rows, so any Deploy instance answers any request.

``CITY_DEPLOY_STORE=sql`` selects it; ``snapshot`` (the default) keeps the in-process
dictionaries and the ``deploy_state`` checkpoint.

A deployment that this instance is changing stays in process only until the request's
checkpoint, or while its pipeline runs here, and is written back on every checkpoint.
A pipeline runs under a fenced lease on its row (T12). Every write names the fence this
instance last saw, so an instance whose lease was taken over writes nothing.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from cap.core.errors import CapError
from cap.core.keys import PublicKey

from city_deploy.agent_ids import ID_FAMILIES, AgentIds, new_rid
from city_deploy.clock import Clock
from city_deploy.records import (
    Deployment,
    DeviceChallenge,
    TeamSession,
    deployment_blob,
    deployment_from,
    device_blob,
    device_from,
    session_blob,
    session_from,
)
from city_deploy.secrets import OVERLAP, KeyRing

STORES = ("snapshot", "sql")
SESSION_TTL = timedelta(days=30)
DEVICE_TTL = timedelta(minutes=15)
# The owner renews every 10 s; a lease not renewed for 45 s is free for another instance.
LEASE = timedelta(seconds=45)
# Phases with a pipeline behind them. Another instance resumes these after the lease expires.
PIPELINE_PHASES = frozenset({"building", "conformance"})


def deploy_store_kind(environ: Mapping[str, str]) -> str:
    raw = environ.get("CITY_DEPLOY_STORE", "").strip().lower() or "snapshot"
    if raw not in STORES:
        raise CapError("SCHEMA_INVALID", "CITY_DEPLOY_STORE")
    return raw


class DeploymentRowLike(Protocol):
    @property
    def deployment_id(self) -> str: ...
    @property
    def phase(self) -> str: ...
    @property
    def body(self) -> dict[str, Any]: ...
    @property
    def fence(self) -> int: ...


class DeviceRowLike(Protocol):
    @property
    def state(self) -> str: ...
    @property
    def body(self) -> dict[str, Any]: ...


class DeployRows(Protocol):
    """The row store Deploy needs (``city_adapters.deploy_rows.RelationalDeployRows``)."""

    def put_session(
        self, token: str, team_id: str, body: Mapping[str, Any], expires_at: datetime
    ) -> None: ...
    def session(self, token: str, now: datetime) -> tuple[str, dict[str, Any]] | None: ...
    def team_session_count(self, team_id: str) -> int: ...
    def drop_team_sessions(self, team_id: str) -> int: ...
    def add_device(
        self, device_code: str, user_code: str, body: Mapping[str, Any], expires_at: datetime
    ) -> None: ...
    def device(self, device_code: str) -> DeviceRowLike | None: ...
    def pending_device(
        self, user_code: str, now: datetime
    ) -> tuple[str, dict[str, Any]] | None: ...
    def approve_device(
        self, code_digest: str, team_id: str, body: Mapping[str, Any], now: datetime
    ) -> bool: ...
    def use_device(self, device_code: str) -> bool: ...
    def drop_team_devices(self, team_id: str) -> int: ...
    def add_deployment(
        self,
        deployment_id: str,
        *,
        team_id: str,
        agent_id: str,
        version: str,
        phase: str,
        body: Mapping[str, Any],
        queued_at: datetime,
    ) -> None: ...
    def deployment(self, deployment_id: str) -> DeploymentRowLike | None: ...
    def all_deployments(self) -> Sequence[DeploymentRowLike]: ...
    def team_deployments(self, team_id: str) -> Sequence[DeploymentRowLike]: ...
    def write(
        self,
        deployment_id: str,
        fence: int,
        *,
        phase: str,
        body: Mapping[str, Any],
        now: datetime,
    ) -> bool: ...
    def claim(
        self, deployment_id: str, holder: str, now: datetime, until: datetime
    ) -> int | None: ...
    def renew(self, deployment_id: str, holder: str, fence: int, until: datetime) -> bool: ...
    def release(self, deployment_id: str, holder: str, fence: int) -> bool: ...
    def claimable(self, now: datetime) -> list[str]: ...
    def admit(
        self,
        deployment_id: str,
        fence: int,
        *,
        limit: int,
        per_team: int,
        now: datetime,
    ) -> int | None: ...
    def leave(self, deployment_id: str, fence: int) -> bool: ...
    def lock(
        self, name: str, holder: str, *, exclusive: bool, now: datetime, until: datetime
    ) -> bool: ...
    def renew_lock(self, name: str, holder: str, until: datetime) -> bool: ...
    def unlock(self, name: str, holder: str) -> bool: ...
    def drop_agent_deployments(self, agent_id: str) -> int: ...
    def put_placement(self, agent_id: str, body: Mapping[str, Any], now: datetime) -> None: ...
    def placement(self, agent_id: str) -> dict[str, Any] | None: ...
    def placements(self) -> dict[str, dict[str, Any]]: ...
    def drop_placement(self, agent_id: str) -> int: ...
    def mint_agent_id(self, team_id: str, family: str, candidate: str) -> str: ...
    def agent_ids_of(self, team_id: str) -> dict[str, str]: ...
    def owner_of(self, agent_id: str) -> str | None: ...
    def is_retired(self, agent_id: str) -> bool: ...
    def retire_agent_id(self, agent_id: str) -> None: ...
    def put_key(
        self, agent_id: str, kid: str, x: str, *, slot: str, overlap_until: datetime | None
    ) -> None: ...
    def keys(self, agent_id: str) -> list[dict[str, str | None]]: ...
    def drop_key(self, agent_id: str, kid: str) -> bool: ...
    def drop_keys(self, agent_id: str) -> int: ...
    def add_secret_name(self, agent_id: str, name: str) -> None: ...
    def secret_names(self, agent_id: str) -> set[str]: ...
    def drop_secret_names(self, agent_id: str) -> int: ...
    def audit(self, message: str, agent_id: str, at: datetime) -> None: ...


def phase_of(deployment: Deployment) -> str:
    if deployment.status == "failed":
        return "failed"
    if deployment.outcome is not None:
        return "done"
    return deployment.status


@dataclass(frozen=True)
class _Write:
    deployment_id: str
    phase: str
    body: dict[str, object]
    text: str
    fence: int


class SharedDeployments:
    """Deployment rows, with the ones this instance is changing held in process."""

    def __init__(self, rows: DeployRows, clock: Clock, holder: str) -> None:
        self._rows = rows
        self._clock = clock
        self.holder = holder
        self._live: dict[str, Deployment] = {}
        self._written: dict[str, str] = {}
        self._running: set[str] = set()
        # Claimed for a pipeline that has not started yet; a checkpoint must not release it.
        self._starting: set[str] = set()
        # The fence each held row had when read here; a claim replaces it.
        self._fences: dict[str, int] = {}
        self._leased: set[str] = set()

    def add(self, deployment: Deployment) -> None:
        body = deployment_blob(deployment)
        self._rows.add_deployment(
            deployment.deployment_id,
            team_id=deployment.team_id,
            agent_id=deployment.agent_id,
            version=deployment.version,
            phase=phase_of(deployment),
            body=body,
            queued_at=deployment.created_at,
        )

    def get(self, deployment_id: str, *, hold: bool = False) -> Deployment | None:
        """The held object when this instance is changing it; otherwise the row.

        ``hold`` keeps the row in process for a caller about to change it."""

        found = self._live.get(deployment_id)
        if found is not None:
            return found
        row = self._rows.deployment(deployment_id)
        if row is None:
            return None
        found = deployment_from(row.body)
        if hold:
            self._live[deployment_id] = found
            self._written[deployment_id] = _canonical(row.body)
            self._fences[deployment_id] = row.fence
        return found

    def team(self, team_id: str) -> list[Deployment]:
        return [self._overlay(row) for row in self._rows.team_deployments(team_id)]

    def every(self) -> list[Deployment]:
        return [self._overlay(row) for row in self._rows.all_deployments()]

    def _overlay(self, row: DeploymentRowLike) -> Deployment:
        held = self._live.get(row.deployment_id)
        return held if held is not None else deployment_from(row.body)

    def drop_agent(self, agent_id: str) -> None:
        self._rows.drop_agent_deployments(agent_id)
        for deployment_id in [k for k, v in self._live.items() if v.agent_id == agent_id]:
            self._forget(deployment_id)

    def _forget(self, deployment_id: str) -> None:
        self._live.pop(deployment_id, None)
        self._written.pop(deployment_id, None)
        self._running.discard(deployment_id)
        self._starting.discard(deployment_id)
        self._fences.pop(deployment_id, None)
        self._leased.discard(deployment_id)

    # Leases.

    def claim(self, deployment: Deployment, *, starting: bool = False) -> bool:
        """Take the pipeline lease. False when another instance holds a live one.

        ``starting`` keeps the lease through the awaits before ``running``."""

        now = self._clock.now()
        fence = self._rows.claim(deployment.deployment_id, self.holder, now, now + LEASE)
        if fence is None:
            return False
        self._live[deployment.deployment_id] = deployment
        self._written.setdefault(deployment.deployment_id, "")
        self._fences[deployment.deployment_id] = fence
        self._leased.add(deployment.deployment_id)
        if starting:
            self._starting.add(deployment.deployment_id)
        return True

    def fence(self, deployment_id: str) -> int:
        return self._fences.get(deployment_id, 0)

    def leases(self) -> list[tuple[str, int]]:
        """Read on the event loop; renewed off it."""

        return [(item, self._fences.get(item, 0)) for item in sorted(self._leased)]

    def renew(self, leases: list[tuple[str, int]]) -> list[str]:
        """The deployments whose lease another instance took. Runs off the event loop."""

        until = self._clock.now() + LEASE
        return [
            item for item, fence in leases if not self._rows.renew(item, self.holder, fence, until)
        ]

    def lose(self, deployment_ids: list[str]) -> None:
        for deployment_id in deployment_ids:
            self._forget(deployment_id)

    def release(self, leases: list[tuple[str, int]]) -> None:
        for deployment_id, fence in leases:
            self._rows.release(deployment_id, self.holder, fence)

    def sweepable(self) -> list[str]:
        """Open pipelines with no live lease that no pipeline here is running."""

        return [
            item
            for item in self._rows.claimable(self._clock.now())
            if item not in self._running and item not in self._starting
        ]

    def pipeline_row(self, deployment_id: str) -> Deployment | None:
        row = self._rows.deployment(deployment_id)
        if row is None or row.phase not in PIPELINE_PHASES:
            return None
        return deployment_from(row.body)

    def stranded_row(self, deployment_id: str) -> Deployment | None:
        """An upload a pipeline claimed (fence above 0) and then lost before ``building``.

        A waiting upload was never claimed, so its fence is still 0."""

        row = self._rows.deployment(deployment_id)
        if row is None or row.phase != "uploading" or row.fence < 1:
            return None
        return deployment_from(row.body)

    # Pipelines and checkpoints.

    def running(self, deployment: Deployment) -> None:
        self._live[deployment.deployment_id] = deployment
        self._starting.discard(deployment.deployment_id)
        self._running.add(deployment.deployment_id)

    def finished(self, deployment: Deployment) -> None:
        self._starting.discard(deployment.deployment_id)
        self._running.discard(deployment.deployment_id)

    def is_running(self, deployment_id: str) -> bool:
        return deployment_id in self._running

    def agent_busy(self, agent_id: str) -> bool:
        """A pipeline here is changing this agent's placement book."""

        return any(self._live[item].agent_id == agent_id for item in self._running)

    def changes(self) -> list[_Write]:
        """What changed since the last write. Read on the event loop, written off it."""

        found: list[_Write] = []
        for deployment_id, deployment in self._live.items():
            body = deployment_blob(deployment)
            text = _canonical(body)
            if self._written.get(deployment_id) != text:
                fence = self._fences.get(deployment_id, 0)
                found.append(_Write(deployment_id, phase_of(deployment), body, text, fence))
        return found

    def write(self, changes: list[_Write]) -> list[str]:
        """Write ``changes``; return the ones refused because the fence is stale."""

        now = self._clock.now()
        refused: list[str] = []
        for change in changes:
            if not self._rows.write(
                change.deployment_id, change.fence, phase=change.phase, body=change.body, now=now
            ):
                refused.append(change.deployment_id)
        return refused

    def settle(self, changes: list[_Write], refused: list[str]) -> list[tuple[str, int]]:
        """Record what was written and let go of deployments no pipeline here is changing.

        One still changed after its write stays held for the next checkpoint. Returns the
        leases to release: their pipeline ended here and its last state is written."""

        self.lose(refused)
        for change in changes:
            if change.deployment_id in self._live:
                self._written[change.deployment_id] = change.text
        released: list[tuple[str, int]] = []
        for deployment_id, deployment in list(self._live.items()):
            if deployment_id in self._running or deployment_id in self._starting:
                continue
            if self._written.get(deployment_id) == _canonical(deployment_blob(deployment)):
                if deployment_id in self._leased:
                    released.append((deployment_id, self._fences.get(deployment_id, 0)))
                self._forget(deployment_id)
        return released

    @property
    def busy(self) -> bool:
        return bool(self._running)


class SharedState:
    """Sessions, device logins, deployments, secret names, and audit, in SQL."""

    def __init__(self, rows: DeployRows, clock: Clock, holder: str) -> None:
        self.rows = rows
        self.clock = clock
        self.deployments = SharedDeployments(rows, clock, holder)

    # Sessions. The token is the caller's; the row holds its digest.

    def session(self, token: str) -> TeamSession | None:
        found = self.rows.session(token, self.clock.now())
        if found is None:
            return None
        team_id, body = found
        return session_from({**body, "team_id": team_id})

    def put_session(self, token: str, session: TeamSession) -> None:
        expires = self.clock.now() + SESSION_TTL
        self.rows.put_session(token, session.team_id, session_blob(session), expires)

    def team_sessions(self, team_id: str) -> int:
        return self.rows.team_session_count(team_id)

    def drop_team(self, team_id: str) -> None:
        self.rows.drop_team_sessions(team_id)
        self.rows.drop_team_devices(team_id)

    # Device login. A code is approved once and exchanged once, on any instance.

    def add_device(self, device_code: str, challenge: DeviceChallenge) -> None:
        expires = self.clock.now() + DEVICE_TTL
        self.rows.add_device(device_code, challenge.user_code, device_blob(challenge), expires)

    def pending_device(self, user_code: str) -> tuple[str, DeviceChallenge] | None:
        found = self.rows.pending_device(user_code, self.clock.now())
        if found is None:
            return None
        code_digest, body = found
        return code_digest, device_from(body)

    def approve_device(self, code_digest: str, challenge: DeviceChallenge) -> bool:
        if challenge.team is None:
            raise CapError("SCHEMA_INVALID", "team")
        body = device_blob(challenge)
        return self.rows.approve_device(code_digest, challenge.team.team_id, body, self.clock.now())

    def device(self, device_code: str) -> DeviceChallenge | None:
        """The challenge while pending or approved. A used code is gone."""

        row = self.rows.device(device_code)
        if row is None or row.state == "used":
            return None
        return device_from(row.body)

    def use_device(self, device_code: str) -> bool:
        return self.rows.use_device(device_code)

    # Secret names (never values) and audit.

    def secret_names(self, agent_id: str) -> set[str]:
        return self.rows.secret_names(agent_id)

    def add_secret_name(self, agent_id: str, name: str) -> None:
        self.rows.add_secret_name(agent_id, name)

    def take_secret_names(self, agent_id: str) -> set[str]:
        names = self.rows.secret_names(agent_id)
        self.rows.drop_secret_names(agent_id)
        return names

    def audit(self, message: str, agent_id: str) -> None:
        self.rows.audit(message, agent_id, self.clock.now())


class SharedAgentIds(AgentIds):
    """ADR-085 ids in ``deploy_agent_ids``: two instances minting a pair get one id."""

    def __init__(self, rows: DeployRows, rid: Callable[[], str] = new_rid) -> None:
        super().__init__(rid=rid)
        self._rows = rows

    def assign(self, team_id: str, family: str) -> str:
        if family not in ID_FAMILIES:
            raise CapError("SCHEMA_INVALID", "family")
        found = self._rows.agent_ids_of(team_id).get(family)
        if found is not None:
            return found
        return self._rows.mint_agent_id(team_id, family, f"agent:{self.rid()}")

    def reserve(self, team_id: str, family: str, agent_id: str) -> None:
        holder = self.team_of(agent_id)
        current = self.get(team_id, family)
        if self._rows.is_retired(agent_id) or (holder is not None and current != agent_id):
            raise CapError("DUPLICATE", "agent id")
        if current is not None and current != agent_id:
            raise CapError("DUPLICATE", "family already has an id")
        if self._rows.mint_agent_id(team_id, family, agent_id) != agent_id:
            raise CapError("DUPLICATE", "family already has an id")

    def get(self, team_id: str, family: str) -> str | None:
        return self._rows.agent_ids_of(team_id).get(family)

    def team_of(self, agent_id: str) -> str | None:
        if self._rows.is_retired(agent_id):
            return None
        return self._rows.owner_of(agent_id)

    def of_team(self, team_id: str) -> dict[str, str]:
        return self._rows.agent_ids_of(team_id)

    def retire(self, agent_id: str) -> None:
        self._rows.retire_agent_id(agent_id)

    def blob(self) -> dict[str, object]:
        return {"pairs": [], "retired": []}

    def load(self, value: object) -> None:
        del value


class SharedKeyRing(KeyRing):
    """Public agent keys in ``deploy_agent_keys``: current, previous, and pending."""

    def __init__(self, rows: DeployRows) -> None:
        super().__init__()
        self._rows = rows

    def _slot(self, agent_id: str, slot: str) -> dict[str, str | None] | None:
        return next((row for row in self._rows.keys(agent_id) if row["slot"] == slot), None)

    def current_x(self, agent_id: str) -> str | None:
        row = self._slot(agent_id, "current")
        return None if row is None else row["x"]

    def install(self, agent_id: str, public: PublicKey, *, now: datetime) -> None:
        del now
        if self.current_x(agent_id) is None:
            x = public.to_jwk()["x"]
            self._rows.put_key(agent_id, "current", x, slot="current", overlap_until=None)

    def rotate(self, agent_id: str, public: PublicKey, *, now: datetime) -> None:
        current = self.current_x(agent_id)
        if current is None:
            raise CapError("KEY_UNKNOWN", agent_id)
        until = now + OVERLAP
        self._rows.put_key(agent_id, "previous", current, slot="previous", overlap_until=until)
        x = public.to_jwk()["x"]
        self._rows.put_key(agent_id, "current", x, slot="current", overlap_until=None)

    def accepts(self, agent_id: str, public: PublicKey, *, now: datetime) -> bool:
        x = public.to_jwk()["x"]
        if self.current_x(agent_id) == x:
            return True
        previous = self._slot(agent_id, "previous")
        if previous is None or previous["x"] != x or previous["overlap_until"] is None:
            return False
        return now < datetime.fromisoformat(previous["overlap_until"])

    def remember_pending(self, agent_id: str, public: PublicKey) -> None:
        x = public.to_jwk()["x"]
        self._rows.put_key(agent_id, "pending", x, slot="pending", overlap_until=None)

    def take_pending(self, agent_id: str) -> PublicKey | None:
        row = self._slot(agent_id, "pending")
        if row is None or row["x"] is None or not self._rows.drop_key(agent_id, "pending"):
            return None
        return PublicKey.from_jwk({"kty": "OKP", "crv": "Ed25519", "x": row["x"]})

    def forget(self, agent_id: str) -> None:
        self._rows.drop_keys(agent_id)


def _canonical(body: Mapping[str, object]) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
