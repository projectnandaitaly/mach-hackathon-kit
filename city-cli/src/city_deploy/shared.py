"""ADR-109: Deploy state as SQL rows, so any Deploy instance answers any request.

``CITY_DEPLOY_STORE=sql`` selects it; ``snapshot`` (the default) keeps the in-process
dictionaries and the ``deploy_state`` checkpoint.

A deployment that this instance is changing stays in process only until the request's
checkpoint, or while its pipeline runs here, and is written back on every checkpoint.
Writes carry fence 0 until pipelines take a lease (T12), so until then one instance runs.
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
    def drop_agent_deployments(self, agent_id: str) -> int: ...
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


class SharedDeployments:
    """Deployment rows, with the ones this instance is changing held in process."""

    def __init__(self, rows: DeployRows, clock: Clock) -> None:
        self._rows = rows
        self._clock = clock
        self._live: dict[str, Deployment] = {}
        self._written: dict[str, str] = {}
        self._running: set[str] = set()

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
            self._live.pop(deployment_id, None)
            self._written.pop(deployment_id, None)
            self._running.discard(deployment_id)

    def running(self, deployment: Deployment) -> None:
        self._live[deployment.deployment_id] = deployment
        self._running.add(deployment.deployment_id)

    def finished(self, deployment: Deployment) -> None:
        self._running.discard(deployment.deployment_id)

    def changes(self) -> list[_Write]:
        """What changed since the last write. Read on the event loop, written off it."""

        found: list[_Write] = []
        for deployment_id, deployment in self._live.items():
            body = deployment_blob(deployment)
            text = _canonical(body)
            if self._written.get(deployment_id) != text:
                found.append(_Write(deployment_id, phase_of(deployment), body, text))
        return found

    def write(self, changes: list[_Write]) -> None:
        now = self._clock.now()
        for change in changes:
            self._rows.write(change.deployment_id, 0, phase=change.phase, body=change.body, now=now)

    def settle(self, changes: list[_Write]) -> None:
        """Record what was written and let go of deployments no pipeline here is changing.

        One still changed after its write stays held for the next checkpoint."""

        for change in changes:
            if change.deployment_id in self._live:
                self._written[change.deployment_id] = change.text
        for deployment_id, deployment in list(self._live.items()):
            if deployment_id in self._running:
                continue
            if self._written.get(deployment_id) == _canonical(deployment_blob(deployment)):
                self._live.pop(deployment_id, None)
                self._written.pop(deployment_id, None)

    @property
    def busy(self) -> bool:
        return bool(self._running)

    def fail_interrupted(self, interrupted: Callable[[Deployment], None]) -> int:
        """Start-up, one instance: a pipeline that was building or testing is gone."""

        count = 0
        now = self._clock.now()
        for row in self._rows.all_deployments():
            if row.phase not in {"building", "conformance"}:
                continue
            deployment = deployment_from(row.body)
            interrupted(deployment)
            self._rows.write(
                deployment.deployment_id,
                0,
                phase=phase_of(deployment),
                body=deployment_blob(deployment),
                now=now,
            )
            count += 1
        return count


class SharedState:
    """Sessions, device logins, deployments, secret names, and audit, in SQL."""

    def __init__(self, rows: DeployRows, clock: Clock) -> None:
        self.rows = rows
        self.clock = clock
        self.deployments = SharedDeployments(rows, clock)

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
