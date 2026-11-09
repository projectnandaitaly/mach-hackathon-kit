"""Deploy workflow. The Index visibility machine is used as it stands."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from secrets import token_hex, token_urlsafe
from typing import Any, ParamSpec, Protocol, TypeVar
from uuid import uuid4

import httpx
from cap.core.envelope import build, sign
from cap.core.errors import CapError
from cap.core.grant import SignedGrant, verify
from cap.core.keys import PublicKey
from city.testkit import ephemeral
from city_conform.runner import Outcome, Report
from city_index.families import family_of, role_permissions
from city_index.model import AgentRecord
from city_index.service import IndexService

from city_deploy.agent_ids import AgentIds
from city_deploy.build import ImageBuilder
from city_deploy.clock import Clock, SystemClock
from city_deploy.conform import ReferenceSuite, Suite
from city_deploy.errors import Conflict, QuotaExceeded
from city_deploy.image import Recipe, SourceFile, parse_dockerfile
from city_deploy.quota import (
    MAX_IMAGE_BYTES,
    assert_build,
    assert_create,
    check_scale,
    recent,
)
from city_deploy.records import (
    Deployment as Deployment,
)
from city_deploy.records import (
    DeviceChallenge as DeviceChallenge,
)
from city_deploy.records import (
    TeamSession as TeamSession,
)
from city_deploy.records import any_map as _any_map
from city_deploy.records import deployment_blob as _deployment_blob
from city_deploy.records import (
    deployment_from,
    device_from,
)
from city_deploy.records import device_blob as _device_blob
from city_deploy.records import object_map as _object_map
from city_deploy.records import session_blob as _session_blob
from city_deploy.records import session_from as _session
from city_deploy.records import text as _text
from city_deploy.records import texts as _texts
from city_deploy.rollouts import RolloutGate
from city_deploy.runtime import (
    DRIVERS,
    EGRESS,
    GkeRuntime,
    MemoryRuntime,
    Revision,
    RolloutFailed,
    Runtime,
    Scale,
    canonical_image,
    namespace_for,
)
from city_deploy.secrets import FIXED_SECRETS, KeyRing, SecretBox, SecretPort
from city_deploy.shared import DeployRows, SharedAgentIds, SharedKeyRing, SharedState
from city_deploy.state import StateSink
from city_deploy.team_llm import MemoryTeamKeys, TeamKeysPort
from city_deploy.teams import OpenTeamDirectory, TeamDirectory

_P = ParamSpec("_P")
_T = TypeVar("_T")
_AGENT = re.compile(r"^agent:[a-z0-9_-]+(?:/[a-z0-9_-]+)?$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_TEAM = re.compile(r"^team:[a-z0-9_-]+$")
_DEFAULT_DEVICE_URI = "https://projectnandaitaly.ai/device"
# The CLI matches this message to re-sign an ``invoke`` grant.
UNASSIGNED = "role permissions need an ASST assignment"
_PKCE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_CARD_FIELDS = (
    "agent_id",
    "team",
    "name",
    "version",
    "cap_versions",
    "profiles",
    "keys",
    "endpoints",
)
_LOG = logging.getLogger("city.deploy")
_TEAM_SLUG = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class EvidenceReader(Protocol):
    """City evidence for one agent: sent rows are market messages it posted."""

    def snapshot(self, agent_id: str) -> Mapping[str, object]: ...


def base_image_ok(dockerfile: str) -> bool:
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return stripped == "FROM city-sdk" or stripped.startswith("FROM city-sdk:")
    return False


def _approve(
    challenge: DeviceChallenge, team_id: str, permissions: list[str], *, loopback: bool
) -> dict[str, str]:
    # A one-click approve must never bind a pollable challenge, or a
    # crafted link would hand the session to whoever started it.
    if loopback and challenge.code_challenge is None:
        raise CapError("POLICY_VIOLATION", "loopback login required")
    challenge.team = TeamSession(team_id, frozenset(permissions))
    if challenge.code_challenge is None:
        return {"status": "approved"}
    challenge.auth_code = token_urlsafe(24)
    return {"status": "approved", "auth_code": challenge.auth_code}


def _outcome_of(report: Report) -> str:
    if any(row.outcome is Outcome.ERROR for row in report.rows):
        return "ERROR"
    if not report.passed:
        return "FAIL"
    return "PASS"


def _suite_detail(report: Report, *, label: str = "conformance") -> str:
    """One-line reason for humans and the CLI ✗ row under on-the-wire failed."""
    outcome = _outcome_of(report)
    bad = [row for row in report.rows if row.outcome in {Outcome.FAIL, Outcome.ERROR}]
    if not bad:
        return f"{label} {outcome}"
    first = bad[0]
    piece = first.name
    if first.detail:
        piece = f"{piece}: {first.detail}"
    if len(bad) > 1:
        piece = f"{piece} (+{len(bad) - 1} more)"
    return f"{label} {outcome} · {piece}"


class DeployService:
    def __init__(
        self,
        *,
        index: IndexService | None = None,
        clock: Clock | None = None,
        suite: Suite | None = None,
        runtime: Runtime | None = None,
        state: StateSink | None = None,
        secrets: SecretPort | None = None,
        teams: TeamDirectory | None = None,
        team_keys: TeamKeysPort | None = None,
        approve_token: str | None = None,
        verification_uri: str | None = None,
        builder: ImageBuilder | None = None,
        evidence: EvidenceReader | None = None,
        rows: DeployRows | None = None,
    ) -> None:
        self.builder = builder
        self._jobs: set[asyncio.Task[None]] = set()
        self.index = index or IndexService()
        self.clock = clock or SystemClock()
        self.suite = suite or ReferenceSuite()
        self.runtime: Runtime = runtime or MemoryRuntime()
        self.state = state
        self.evidence = evidence
        self.teams: TeamDirectory = teams if teams is not None else OpenTeamDirectory()
        self.team_keys: TeamKeysPort = team_keys if team_keys is not None else MemoryTeamKeys()
        self.approve_token = approve_token
        self.verification_uri = verification_uri or _DEFAULT_DEVICE_URI
        self.ring = KeyRing()
        self.secrets: SecretPort = secrets if secrets is not None else SecretBox()
        if isinstance(self.runtime, GkeRuntime):
            self.runtime.bind_identity(self.secrets.pod_material)
            self.runtime.bind_llm_key(self._team_llm_key)
        self.sessions: dict[str, TeamSession] = {}
        self.deployments: dict[str, Deployment] = {}
        self.secret_names: dict[str, set[str]] = {}
        self.audit: list[dict[str, str]] = []
        self.conform_runs = 0
        self._devices: dict[str, DeviceChallenge] = {}
        self.market_locked: Callable[[], bool] | None = None
        self._purged: dict[str, list[str]] = {}
        self.agent_ids = AgentIds()
        # ADR-109: with rows, sessions, logins, ids, keys and deployments live in SQL.
        self.shared: SharedState | None = None
        if rows is not None:
            self.shared = SharedState(rows, self.clock)
            self.agent_ids = SharedAgentIds(rows)
            self.ring = SharedKeyRing(rows)
        self._flusher: asyncio.Task[None] | None = None
        self.rollouts = RolloutGate.from_env()
        # One writer keeps checkpoints in order: an older snapshot never lands last.
        self._checkpoints = ThreadPoolExecutor(max_workers=1, thread_name_prefix="checkpoint")

    def now(self) -> datetime:
        return self.clock.now()

    async def _offload(self, call: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> _T:
        """Run a cluster or Secret Manager call on a worker thread when the runtime is remote."""

        if not self.runtime.remote:
            return call(*args, **kwargs)
        return await asyncio.to_thread(call, *args, **kwargs)

    @contextmanager
    def _book_held(self) -> Iterator[None]:
        lock = getattr(self.runtime, "book_lock", None)
        if lock is None:
            yield
            return
        with lock:
            yield

    async def checkpoint(self) -> None:
        """Write changed deployment rows, then the snapshot, off the event loop."""

        await self._flush_rows()
        if self.state is None:
            return
        body = self.snapshot()
        await asyncio.get_running_loop().run_in_executor(
            self._checkpoints, self.state.checkpoint, body
        )

    async def _flush_rows(self) -> None:
        if self.shared is None:
            return
        deployments = self.shared.deployments
        changes = deployments.changes()
        if changes:
            await asyncio.get_running_loop().run_in_executor(
                self._checkpoints, deployments.write, changes
            )
        deployments.settle(changes)

    def _start_flusher(self) -> None:
        """While pipelines run here, other instances see their progress within a second."""

        if self.shared is None or (self._flusher is not None and not self._flusher.done()):
            return
        self._flusher = asyncio.create_task(self._flush_while_busy())

    async def _flush_while_busy(self) -> None:
        assert self.shared is not None
        while self.shared.deployments.busy:
            await asyncio.sleep(1.0)
            try:
                await self._flush_rows()
            except Exception:
                _LOG.exception("deployment rows not written")

    def fail_interrupted(self) -> int:
        """SQL store, one instance: pipelines that were running before a restart are gone."""

        if self.shared is None:
            return 0
        return self.shared.deployments.fail_interrupted(interrupted)

    def _team_llm_key(self, team_id: str) -> str | None:
        """The team's Gateway key for its pods. Do not log the result."""

        return self.team_keys.read_team(team_id) if team_id else None

    def _audit(self, message: str, agent_id: str) -> None:
        if self.shared is not None:
            self.shared.audit(message, agent_id)
            return
        self.audit.append({"message": message, "agent_id": agent_id})

    def _session(self, token: str) -> TeamSession:
        found = self.shared.session(token) if self.shared is not None else self.sessions.get(token)
        if found is None:
            raise CapError("POLICY_VIOLATION", "unknown team")
        return found

    def _put_session(self, token: str, session: TeamSession) -> None:
        if self.shared is not None:
            self.shared.put_session(token, session)
            return
        self.sessions[token] = session

    def _device(self, device_code: str) -> DeviceChallenge | None:
        if self.shared is not None:
            return self.shared.device(device_code)
        return self._devices.get(device_code)

    def _use_device(self, device_code: str) -> bool:
        """Poll and exchange release one token per approved code on the SQL store."""

        if self.shared is not None:
            return self.shared.use_device(device_code)
        return True

    def _add_deployment(self, deployment: Deployment) -> None:
        if self.shared is not None:
            self.shared.deployments.add(deployment)
            return
        self.deployments[deployment.deployment_id] = deployment

    def _deployment(self, deployment_id: str, *, hold: bool = False) -> Deployment | None:
        if self.shared is not None:
            return self.shared.deployments.get(deployment_id, hold=hold)
        return self.deployments.get(deployment_id)

    def _team_rows(self, team_id: str) -> list[Deployment]:
        if self.shared is not None:
            return self.shared.deployments.team(team_id)
        return [item for item in self.deployments.values() if item.team_id == team_id]

    def _all_rows(self) -> list[Deployment]:
        if self.shared is not None:
            return self.shared.deployments.every()
        return list(self.deployments.values())

    def _secret_names(self, agent_id: str) -> set[str]:
        if self.shared is not None:
            return self.shared.secret_names(agent_id)
        return set(self.secret_names.get(agent_id, ()))

    def _take_secret_names(self, agent_id: str) -> set[str]:
        if self.shared is not None:
            return self.shared.take_secret_names(agent_id)
        return self.secret_names.pop(agent_id, set())

    def start_device(self, code_challenge: str | None = None) -> dict[str, str]:
        if code_challenge is not None and not _PKCE.fullmatch(code_challenge):
            raise CapError("SCHEMA_INVALID", "code_challenge")
        device_code = token_urlsafe(16)
        user_code = token_hex(3)
        challenge = DeviceChallenge(user_code, code_challenge=code_challenge)
        if self.shared is not None:
            self.shared.add_device(device_code, challenge)
        else:
            self._devices[device_code] = challenge
        return {
            "device_code": device_code,
            "user_code": user_code,
            "verification_uri": self.verification_uri,
        }

    def authorize_approve(self, authorization: str | None) -> None:
        """When an approve token is configured, require a matching Bearer."""
        expected = self.approve_token
        if expected is None:
            return
        if authorization is None or not authorization.startswith("Bearer "):
            raise CapError("POLICY_VIOLATION", "approve token required")
        token = authorization.removeprefix("Bearer ").strip()
        if not token or len(token) != len(expected) or not _token_eq(token, expected):
            raise CapError("POLICY_VIOLATION", "approve token required")

    def approve_device(
        self,
        user_code: str,
        team_id: str,
        permissions: list[str],
        *,
        loopback: bool = False,
    ) -> dict[str, str]:
        if not team_id or not _TEAM.fullmatch("team:" + team_id):
            raise CapError("SCHEMA_INVALID", "team")
        self.teams.require(team_id)
        if self.shared is not None:
            found = self.shared.pending_device(user_code)
            if found is None:
                raise CapError("SCHEMA_INVALID", "user code")
            code_digest, challenge = found
            approved = _approve(challenge, team_id, permissions, loopback=loopback)
            if not self.shared.approve_device(code_digest, challenge):
                raise CapError("SCHEMA_INVALID", "user code")
            return approved
        for challenge in self._devices.values():
            if challenge.user_code == user_code and challenge.team is None:
                return _approve(challenge, team_id, permissions, loopback=loopback)
        raise CapError("SCHEMA_INVALID", "user code")

    def poll_device(self, device_code: str) -> dict[str, str]:
        challenge = self._device(device_code)
        if challenge is not None and challenge.code_challenge is not None:
            raise CapError("POLICY_VIOLATION", "loopback login uses /login/token")
        if challenge is None or challenge.team is None or not self._use_device(device_code):
            raise CapError("POLICY_VIOLATION", "authorization pending")
        token = token_urlsafe(16)
        self._put_session(token, challenge.team)
        return {"token": token, "team_id": challenge.team.team_id}

    def exchange_device(
        self, device_code: str, auth_code: str, code_verifier: str
    ) -> dict[str, str]:
        challenge = self._device(device_code)
        if (
            challenge is None
            or challenge.team is None
            or challenge.code_challenge is None
            or challenge.auth_code is None
        ):
            raise CapError("POLICY_VIOLATION", "authorization pending")
        if not _token_eq(auth_code, challenge.auth_code):
            raise CapError("SIGNATURE_INVALID", "auth code")
        if not _token_eq(_pkce_s256(code_verifier), challenge.code_challenge):
            raise CapError("SIGNATURE_INVALID", "code verifier")
        if self.shared is None:
            del self._devices[device_code]
        elif not self.shared.use_device(device_code):
            raise CapError("POLICY_VIOLATION", "authorization pending")
        token = token_urlsafe(16)
        self._put_session(token, challenge.team)
        return {"token": token, "team_id": challenge.team.team_id}

    def register_controller(
        self,
        token: str,
        jwk: Mapping[str, object],
        *,
        retire: Mapping[str, object] | None = None,
    ) -> dict[str, str]:
        """Register this teammate's controller key. Other teammates' keys stay."""

        session = self._session(token)
        retire_x = None if retire is None else _public_x(retire)
        stored = self.index.catalog.put_controller(
            "team:" + session.team_id, jwk, retire_x=retire_x
        )
        session.controller = PublicKey.from_jwk(stored)
        if self.shared is not None:
            self.shared.put_session(token, session)
        return stored

    def team_llm_key(self, token: str) -> dict[str, str]:
        """Ensure and return the team gateway VK (lazy backfill for existing teams)."""
        session = self._session(token)
        value = self.team_keys.ensure_team(session.team_id)
        return {"team_id": session.team_id, "virtual_key": value}

    def assign_agent_id(self, token: str, family: str) -> dict[str, str]:
        """ADR-085: the team's id for ``family``, minted on first use."""

        session = self._session(token)
        agent_id = self.agent_ids.assign(session.team_id, family)
        self._audit("agent id assigned", agent_id)
        return {"agent_id": agent_id, "family": family, "team_id": session.team_id}

    def list_agent_ids(self, token: str) -> dict[str, Any]:
        session = self._session(token)
        rows = [
            {"agent_id": agent_id, "family": family, "team_id": session.team_id}
            for family, agent_id in sorted(self.agent_ids.of_team(session.team_id).items())
        ]
        return {"team_id": session.team_id, "agent_ids": rows}

    def owner_of(self, agent_id: str) -> str | None:
        """The team that owns ``agent_id``: Deploy's ids first, then the Index record."""

        found = self.agent_ids.team_of(agent_id)
        if found is not None:
            return found
        record = self._existing(agent_id)
        if record is None:
            staged = self.index.catalog.staged(agent_id)
            record = staged
        if record is None:
            return None
        return record.team_id.removeprefix("team:")

    def mint_agent_key(self, token: str, agent_id: str) -> dict[str, str]:
        """Return the ring public JWK. First call mints; later calls are idempotent."""
        session = self._session(token)
        _require_agent(agent_id)
        if self.agent_ids.team_of(agent_id) != session.team_id:
            raise CapError("GRANT_PERMISSION_DENIED", "agent id is not the team's")
        current = self.ring.current_x(agent_id)
        if current is not None:
            return {"kty": "OKP", "crv": "Ed25519", "x": current}
        pair = ephemeral()
        self.secrets.hold_agent_key(agent_id, pair.private)
        self.ring.install(agent_id, pair.public, now=self.now())
        self._audit("agent key minted", agent_id)
        return pair.public.to_jwk()

    def prepare_rotation(self, token: str, agent_id: str) -> dict[str, str]:
        self._session(token)
        if self.ring.current_x(agent_id) is None:
            raise CapError("KEY_UNKNOWN", agent_id)
        pair = ephemeral()
        self.secrets.hold_agent_key(agent_id, pair.private)
        if isinstance(self.ring, SharedKeyRing):
            self.ring.remember_pending(agent_id, pair.public)
        else:
            self.secrets.remember_pending(agent_id, pair.public)
        self._audit("agent key prepared", agent_id)
        return pair.public.to_jwk()

    def create_deployment(self, token: str, body: Mapping[str, Any]) -> dict[str, Any]:
        session = self._session(token)
        card = _card(body.get("card"))
        grant = _grant_body(body.get("grant"))
        jws = body.get("jws")
        if not isinstance(jws, str) or not jws:
            raise CapError("SIGNATURE_INVALID", "jws is required")
        runtime_name = body.get("runtime", "memory")
        if not isinstance(runtime_name, str) or runtime_name not in DRIVERS:
            raise CapError("SCHEMA_INVALID", "runtime")
        promote = body.get("promote", False)
        if not isinstance(promote, bool):
            raise CapError("SCHEMA_INVALID", "promote")
        self._check_grant(session, card, grant, jws)
        agent_id = str(card["agent_id"])
        self.secrets.set_secret(agent_id, "grant", _grant_document(grant, jws))
        version = str(card["version"])
        self._assert_quota(session.team_id, agent_id)
        deployment_id = "dep-" + uuid4().hex
        deployment = Deployment(
            deployment_id=deployment_id,
            team_id=session.team_id,
            agent_id=agent_id,
            version=version,
            card=card,
            status="uploading",
            upload_url=f"https://city.example/uploads/{deployment_id}",
            created_at=self.now(),
            runtime_name=runtime_name,
            promote=promote,
        )
        self._add_deployment(deployment)
        self._audit("deployment accepted", agent_id)
        return deployment.view()

    async def complete_upload(
        self,
        token: str,
        deployment_id: str,
        *,
        dockerfile: str,
        size_bytes: int,
        image_digest: str | None = None,
        files: Mapping[str, SourceFile] | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        session = self._session(token)
        deployment = self._owned(session, deployment_id, hold=True)
        if deployment.status != "uploading":
            return deployment.view()
        if not base_image_ok(dockerfile) or size_bytes > MAX_IMAGE_BYTES:
            return self._refuse(
                deployment, "FAIL", "build refused", "Dockerfile must start FROM city-sdk"
            )
        pinned = None if image_digest is None else _pinned_digest(image_digest)
        recipe: Recipe | None = None
        if pinned is None and self.builder is not None and files is not None:
            try:
                recipe = parse_dockerfile(dockerfile)
            except CapError as exc:
                return self._refuse(deployment, "FAIL", "build refused", exc.message)
        elif pinned is None and self.runtime.remote:
            # A remote runtime pulls from Artifact Registry; a digest nobody pushed never starts.
            if self.builder is None:
                raise CapError("POLICY_VIOLATION", "agent image build is not configured")
            raise CapError("POLICY_VIOLATION", "source upload required: upgrade city-cli")
        building = sum(1 for item in self._team_rows(session.team_id) if item.status == "building")
        try:
            assert_build(building=building)
        except QuotaExceeded:
            deployment.status = "failed"
            deployment.outcome = "FAIL"
            raise
        deployment.status = "building"
        job = self._held(
            deployment, self._pipeline(deployment, dockerfile, pinned, recipe, files or {})
        )
        if wait:
            return await job
        task = asyncio.create_task(self._background(deployment, job))
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)
        return deployment.view()

    def _held(
        self, deployment: Deployment, job: Awaitable[dict[str, Any]]
    ) -> Awaitable[dict[str, Any]]:
        """Keep the deployment in process while its pipeline runs here."""

        if self.shared is None:
            return job
        deployments = self.shared.deployments
        deployments.running(deployment)
        self._start_flusher()

        async def run() -> dict[str, Any]:
            try:
                return await job
            finally:
                deployments.finished(deployment)

        return run()

    async def settle(self) -> None:
        """Wait for background pipelines. Tests and shutdown use it."""

        while self._jobs:
            await asyncio.gather(*list(self._jobs), return_exceptions=True)

    async def _background(self, deployment: Deployment, job: Awaitable[dict[str, Any]]) -> None:
        try:
            await job
        except Exception:
            _LOG.exception("deploy pipeline crashed", extra={"agent_id": deployment.agent_id})
            self._refuse(deployment, "ERROR", "platform error", "deploy pipeline error")
        finally:
            await self.checkpoint()

    def _refuse(
        self, deployment: Deployment, outcome: str, audit: str, detail: str
    ) -> dict[str, Any]:
        deployment.status = "failed"
        deployment.outcome = outcome
        deployment.step = None
        deployment.detail = detail
        self.index.catalog.drop_stage(deployment.agent_id)
        self._audit(audit, deployment.agent_id)
        return deployment.view()

    def _phase(self, deployment: Deployment, step: str, started: float, *, message: str) -> None:
        _LOG.info(
            message,
            extra={
                "agent_id": deployment.agent_id,
                "deployment_id": deployment.deployment_id,
                "step": step,
                "seconds": round(time.perf_counter() - started, 3),
            },
        )

    async def _pipeline(
        self,
        deployment: Deployment,
        dockerfile: str,
        pinned: str | None,
        recipe: Recipe | None,
        files: Mapping[str, SourceFile],
    ) -> dict[str, Any]:
        if pinned is not None:
            digest = pinned
            deployment.scan = "clean"
        elif recipe is not None and self.builder is not None:
            deployment.step = "build"
            started = time.perf_counter()
            try:
                built = await asyncio.to_thread(
                    self.builder.build,
                    agent_id=deployment.agent_id,
                    version=deployment.version,
                    recipe=recipe,
                    files=files,
                )
            except CapError as exc:
                return self._refuse(deployment, "FAIL", "build refused", exc.message)
            except Exception:
                _LOG.exception("agent image build failed", extra={"agent_id": deployment.agent_id})
                return self._refuse(
                    deployment, "ERROR", "platform error", "agent image build failed"
                )
            digest = built.ref
            # The source layer holds team files only; packages come from the certified base.
            deployment.scan = "base-certified"
            self._phase(
                deployment,
                "build",
                started,
                message="agent image pushed",
            )
        else:
            digest = "sha256:" + hashlib.sha256(dockerfile.encode("utf-8")).hexdigest()
            deployment.scan = "clean"
        deployment.image_digest = digest
        deployment.sbom_id = "sbom:" + digest.rsplit(":", 1)[-1][:12]
        deployment.base_image = "city-sdk"
        deployment.status = "conformance"
        if not self.runtime.remote:
            return await self._roll_and_release(deployment)
        deployment.step = "rollout"
        async with self.rollouts.slot(deployment.team_id, lambda ahead: _queued(deployment, ahead)):
            deployment.detail = None
            return await self._roll_and_release(deployment)

    async def _roll_and_release(self, deployment: Deployment) -> dict[str, Any]:
        profile = self._profile(deployment.agent_id, deployment.version)
        # Remote releases always place a candidate first. Conformance hits that URL;
        # promote retargets the serving route only after PASS. A first release used to
        # roll at 100% and flake on custody.ack (E19-deploy-first-release).
        first_release = self.runtime.serving(deployment.agent_id) is None
        if self.runtime.remote:
            deployment.step = "rollout"
            started = time.perf_counter()
            try:
                await self._apply_traffic(deployment, 1, publish=False, front_door=first_release)
            except RolloutFailed as exc:
                if first_release:
                    await self._close_front_door(deployment)
                await self._pod_failed(deployment, exc)
                return self._refuse(deployment, exc.outcome, "rollout failed", exc.detail)
            except CapError as exc:
                if first_release:
                    await self._close_front_door(deployment)
                return self._refuse(deployment, "ERROR", "rollout failed", exc.message or exc.code)
            self._phase(deployment, "rollout", started, message="deploy phase finished")
        deployment.step = "conformance"
        started = time.perf_counter()
        try:
            report = await self._run_suite(deployment, profile)
        except Exception:
            _LOG.exception(
                "conformance runner failed",
                extra={"agent_id": deployment.agent_id, "version": deployment.version},
            )
            await self._stop_unpassed(deployment, direct=first_release)
            return self._refuse(deployment, "ERROR", "platform error", "conformance runner error")
        self._phase(deployment, "conformance", started, message="deploy phase finished")
        outcome = _outcome_of(report)
        if outcome != "PASS":
            await self._stop_unpassed(deployment, direct=first_release)
            return self._refuse(
                deployment,
                outcome,
                "conformance failed",
                _suite_detail(report),
            )
        prior = self.runtime.serving(deployment.agent_id)
        if deployment.promote:
            deployment.step = "promote"
            started = time.perf_counter()
            try:
                await self._promote_update(deployment)
            except RolloutFailed as exc:
                if first_release:
                    await self._close_front_door(deployment)
                await self._pod_failed(deployment, exc)
                if prior is not None and prior.version != deployment.version:
                    await self._restore(prior)
                return self._refuse(deployment, exc.outcome, "rollout failed", exc.detail)
            self._phase(deployment, "promote", started, message="deploy phase finished")
            gap = self._release_gap(deployment)
            if gap is not None:
                if prior is not None and prior.version != deployment.version:
                    await self._restore(prior)
                else:
                    if first_release:
                        await self._close_front_door(deployment)
                    await self._withdraw(deployment, delete=True)
                return self._refuse(deployment, "ERROR", "release incomplete", gap)
            deployment.status = "promoted"
        else:
            deployment.status = "conformance"
        deployment.step = None
        if deployment.promote:
            await self._scale_down_superseded(deployment.agent_id)
            self._confirm_released(deployment)
        # PASS only when the release finished: Index visible and the new pod serving.
        deployment.outcome = "PASS"
        self._audit("deployment finished", deployment.agent_id)
        return deployment.view()

    def get_deployment(self, token: str, deployment_id: str) -> dict[str, Any]:
        session = self._session(token)
        return self._owned(session, deployment_id).view()

    def team_deployments(self, team_id: str) -> dict[str, Any]:
        """Latest attempt for each agent. A failed first deploy stays on this list."""

        team = self._team_rows(team_id)
        latest: dict[str, Deployment] = {}
        for item in team:
            current = latest.get(item.agent_id)
            if current is None or item.created_at >= current.created_at:
                latest[item.agent_id] = item
        rows = []
        for agent_id in sorted(latest):
            history = sorted(
                (item for item in team if item.agent_id == agent_id),
                key=lambda item: item.created_at,
            )
            body = history[-1].view()
            merged = merged_stage_rows([item.stages for item in history])
            if merged:
                body["conformance"] = merged
            rows.append(body)
        return {"team_id": team_id, "deployments": rows}

    async def rollback(
        self,
        token: str,
        agent_id: str,
        version: str | None,
    ) -> dict[str, Any]:
        self._session(token)
        target = self._rollback_target(agent_id, version)
        if target is None:
            raise Conflict("no certified revision")
        await self._restore(target)
        self._audit("rollback", agent_id)
        return {"agent_id": agent_id, "version": target.version, "traffic": 100}

    def _refuse_live_wave(self) -> None:
        locked = self.market_locked
        if callable(locked) and locked():
            raise CapError("POLICY_VIOLATION", "a wave is open")

    async def delete_agent(
        self, token: str | None, agent_id: str, *, console: bool = False
    ) -> dict[str, Any]:
        """Remove the agent from the Index, the roster, the cluster, and Deploy."""

        if not console:
            session = self._session(token or "")
            if "agent.delete" not in session.permissions:
                raise CapError("GRANT_PERMISSION_DENIED", "captain")
            if self.owner_of(agent_id) != session.team_id:
                raise CapError("GRANT_PERMISSION_DENIED", "team")
        self._refuse_live_wave()
        return await self._erase(agent_id)

    async def _erase(self, agent_id: str) -> dict[str, Any]:
        owner = self.owner_of(agent_id)
        self.index.catalog.drop_stage(agent_id)
        await self.index.delete_agent(agent_id, role_header="deploy", cause="delete")
        drop = getattr(self.teams, "drop_agent", None)
        if callable(drop) and owner is not None:
            await self._offload(drop, agent_id, team_id=owner)
        remove = getattr(self.runtime, "remove_agent", None)
        if callable(remove):
            await self._offload(remove, agent_id)
        self.runtime.revisions.pop(agent_id, None)
        self.runtime.scales.pop(agent_id, None)
        self.runtime.drained.discard(agent_id)
        self.runtime.smoked.discard(agent_id)
        names = self._take_secret_names(agent_id)
        await self._offload(self.secrets.forget_agent, agent_id, names)
        if self.shared is not None:
            self.shared.deployments.drop_agent(agent_id)
        else:
            self.deployments = {
                key: item for key, item in self.deployments.items() if item.agent_id != agent_id
            }
        if isinstance(self.ring, SharedKeyRing):
            self.ring.forget(agent_id)
        self.agent_ids.retire(agent_id)
        self._audit("deleted", agent_id)
        return {"agent_id": agent_id, "deleted": True}

    async def purge_team(self, team_id: str, *, dry_run: bool) -> dict[str, Any]:
        """ADR-050: retire every agent of the team, then drop its namespace, secrets, key,
        and sessions. Each step skips what is already gone, so a second call resumes."""

        if not _TEAM_SLUG.fullmatch(team_id):
            raise CapError("SCHEMA_INVALID", "team_id")
        owned = sorted(
            set(self.agent_ids.of_team(team_id).values())
            | {
                item.agent_id
                for item in self.index.catalog.all_agents()
                if item.team_id == "team:" + team_id
            }
            | {item.agent_id for item in self._team_rows(team_id)}
        )
        if not owned:
            owned = list(self._purged.get(team_id, []))
        sessions = [token for token, item in self.sessions.items() if item.team_id == team_id]
        session_count = len(sessions) if self.shared is None else self.shared.team_sessions(team_id)
        secrets = sum(len({*FIXED_SECRETS, *self._secret_names(a)}) for a in owned)
        team_key = await self._offload(self.team_keys.read_team, team_id) is not None
        if not dry_run:
            self._refuse_live_wave()
            self._purged[team_id] = list(owned)
        rows: list[dict[str, Any]] = []
        for agent_id in owned:
            if not dry_run:
                await self._erase(agent_id)
            rows.append({"agent_id": agent_id, "deleted": True})
        namespace = namespace_for("team:" + team_id)
        if not dry_run:
            remove_namespace = getattr(self.runtime, "remove_namespace", None)
            if callable(remove_namespace):
                await self._offload(remove_namespace, namespace)
            for agent_id in owned:
                names = self._take_secret_names(agent_id)
                await self._offload(self.secrets.forget_agent, agent_id, names)
            team_key = await self._offload(self.team_keys.revoke_team, team_id)
            for token in sessions:
                del self.sessions[token]
            for code, challenge in list(self._devices.items()):
                if challenge.team is not None and challenge.team.team_id == team_id:
                    del self._devices[code]
            if self.shared is not None:
                self.shared.drop_team(team_id)
            self._audit("team purged", "team:" + team_id)
        return {
            "team_id": team_id,
            "dry_run": dry_run,
            "agents": rows,
            "namespace": namespace,
            "secrets": secrets,
            "team_key": team_key,
            "sessions": session_count,
        }

    def status(self, token: str, agent_id: str) -> dict[str, Any]:
        session = self._session(token)
        record = self._existing(agent_id)
        serving = self.runtime.serving(agent_id)
        scale = self.runtime.scales.get(agent_id)
        visibility = None if record is None else record.visibility
        # Prefer the live revision; fall back to Index so a replica without the
        # runtime book still reports the published version (not "deployed no · visible").
        version = None if serving is None else serving.version
        if version is None and record is not None and record.active_version:
            version = record.active_version
        keyed = self.ring.current_x(agent_id) is not None
        candidates = self._all_rows() if self.shared is None else self._team_rows(session.team_id)
        has_deploy = any(item.agent_id == agent_id for item in candidates)
        initialized = keyed or record is not None or has_deploy
        deployed = visibility == "visible" and version is not None
        rating_count = 0 if record is None else record.rating_count
        message_count = self._message_count(agent_id)
        # A visible release always went through conformance, which exchanges
        # messages with city agents — do not depend on per-replica smoked state.
        messaged = (
            deployed or rating_count > 0 or message_count > 0 or agent_id in self.runtime.smoked
        )
        body: dict[str, Any] = {
            "agent_id": agent_id,
            "visibility": visibility,
            "version": version,
            "card_digest": None if record is None else record.card_digest,
            "initialized": initialized,
            "deployed": deployed,
            "messaged": messaged,
            "message_count": message_count,
            "quota": {
                "agents": len(self._team_agents(session.team_id)),
                "deploys_this_hour": len(self._recent_deploys(session.team_id, agent_id)),
            },
        }
        if scale is not None:
            body["scale"] = {
                "min": scale.min_replicas,
                "max": scale.max_replicas,
                "concurrency": scale.concurrency,
            }
        return body

    def _message_count(self, agent_id: str) -> int:
        """How many market messages this agent has posted (city evidence ``sent``)."""

        if self.evidence is not None:
            return _sent_count(self.evidence.snapshot(agent_id))
        base = _evidence_base(self.runtime, agent_id)
        if not base:
            return 0
        try:
            response = httpx.get(
                f"{base}/v1/evidence",
                params={"agent_id": agent_id},
                timeout=5.0,
            )
        except httpx.HTTPError:
            return 0
        if response.status_code != 200:
            return 0
        try:
            body = response.json()
        except ValueError:
            return 0
        if not isinstance(body, dict):
            return 0
        return _sent_count(body)

    def scale(
        self,
        token: str,
        agent_id: str,
        *,
        min_replicas: int,
        max_replicas: int,
        concurrency: int,
    ) -> dict[str, Any]:
        self._session(token)
        check_scale(
            min_replicas=min_replicas,
            max_replicas=max_replicas,
            concurrency=concurrency,
        )
        self.runtime.scales[agent_id] = Scale(min_replicas, max_replicas, concurrency)
        return {
            "agent_id": agent_id,
            "min": min_replicas,
            "max": max_replicas,
            "concurrency": concurrency,
        }

    def set_secret(self, token: str, agent_id: str, name: str, value: str) -> dict[str, str]:
        self._session(token)
        if not name or not value:
            raise CapError("SCHEMA_INVALID", "secret")
        self.secrets.set_secret(agent_id, name, value)
        if self.shared is not None:
            self.shared.add_secret_name(agent_id, name)
        else:
            self.secret_names.setdefault(agent_id, set()).add(name)
        self._audit("secret stored", agent_id)
        return {"agent_id": agent_id, "status": "stored", "key": name}

    def rotate_llm(self, token: str, agent_id: str) -> dict[str, str]:
        self._session(token)
        self.secrets.rotate_llm(agent_id)
        self._audit("llm key rotated", agent_id)
        return {"agent_id": agent_id, "status": "rotated"}

    def revoke_llm(self, token: str, agent_id: str) -> dict[str, str]:
        self._session(token)
        self.secrets.revoke_llm(agent_id)
        self._audit("llm key revoked", agent_id)
        return {"agent_id": agent_id, "status": "revoked"}

    async def confirm_rotation(
        self,
        token: str,
        agent_id: str,
        body: Mapping[str, Any],
    ) -> dict[str, str]:
        session = self._session(token)
        if isinstance(self.ring, SharedKeyRing):
            pending = self.ring.take_pending(agent_id)
        else:
            pending = self.secrets.take_pending(agent_id)
        if pending is None:
            raise CapError("KEY_UNKNOWN", agent_id)
        grant = _grant_body(body.get("grant"))
        jws = body.get("jws")
        if not isinstance(jws, str):
            raise CapError("SIGNATURE_INVALID", "jws is required")
        pubkey = grant.get("agent_pubkey")
        if not isinstance(pubkey, Mapping) or pubkey.get("x") != pending.to_jwk()["x"]:
            raise CapError("SCHEMA_INVALID", "agent_pubkey")
        if grant.get("agent_id") != agent_id:
            raise CapError("SCHEMA_INVALID", "agent_id")
        self._verify_only(session, grant, jws)
        self.ring.rotate(agent_id, pending, now=self.now())
        self._audit("agent key rotated", agent_id)
        return {"agent_id": agent_id, "status": "rotated"}

    def synthetic_need(
        self,
        token: str,
        agent_id: str,
        *,
        recipient: str,
        grant_id: str,
        need_id: str,
        body: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Sign one need with the held agent key. The private key is not returned."""

        session = self._session(token)
        _require_agent(agent_id)
        _require_agent(recipient)
        owned = any(item.agent_id == agent_id for item in self._team_rows(session.team_id))
        if not owned:
            raise CapError("TARGET_NOT_VISIBLE", agent_id)
        if not grant_id or not need_id:
            raise CapError("SCHEMA_INVALID", "need")
        payload: dict[str, object] = {"service": "lab", "summary": "visit"}
        if body is not None:
            payload = {str(key): value for key, value in body.items()}
        unsigned = build(
            msg_id=str(uuid4()),
            message_type="need.request",
            sender=agent_id,
            recipient=recipient,
            sent_at=self.now(),
            body=payload,
            need_id=need_id,
        )
        envelope = sign(
            unsigned,
            self.secrets.agent_key(agent_id),
            kid=f"{agent_id}#k1",
            grant_id=grant_id,
        )
        return {"envelope": envelope}

    def compliance(self, token: str, agent_id: str) -> dict[str, Any]:
        self._session(token)
        serving = self.runtime.serving(agent_id)
        if serving is None:
            raise CapError("TARGET_NOT_VISIBLE", agent_id)
        from city_deploy.receipt import sign_receipt

        envelope, public = sign_receipt(
            agent_id=agent_id,
            image_digest=serving.image_digest,
            now=self.now(),
        )
        return {"receipt": envelope, "public_key": public.to_jwk()}

    def _check_grant(
        self,
        session: TeamSession,
        card: dict[str, Any],
        grant: dict[str, Any],
        jws: str,
    ) -> None:
        if grant.get("agent_id") != card["agent_id"] or grant.get("version") != card["version"]:
            raise CapError("SCHEMA_INVALID", "grant does not match card")
        if card["team"] != "team:" + session.team_id:
            raise CapError("GRANT_PERMISSION_DENIED", "team")
        self._verify_only(session, grant, jws)
        self._check_agent_id(session, card)
        permissions = grant["permissions"]
        if not isinstance(permissions, list):
            raise CapError("SCHEMA_INVALID", "permissions")
        asked = {item for item in permissions if isinstance(item, str)}
        if len(asked) != len(permissions):
            raise CapError("GRANT_PERMISSION_DENIED", "permission")
        self._check_role(session, card, frozenset(asked))
        minted = self.ring.current_x(str(card["agent_id"]))
        pubkey = grant.get("agent_pubkey")
        card_key = card["keys"][0]
        if (
            minted is None
            or not isinstance(pubkey, Mapping)
            or pubkey.get("x") != minted
            or not isinstance(card_key, dict)
            or card_key.get("x") != minted
        ):
            raise CapError("KEY_UNKNOWN", "agent key")

    def _check_agent_id(self, session: TeamSession, card: Mapping[str, Any]) -> None:
        """ADR-085: the card carries an id the city issued to this team.

        One agent per family stays the ADR-044 check, so a card may change family."""

        if self.agent_ids.team_of(str(card["agent_id"])) != session.team_id:
            raise CapError(
                "POLICY_VIOLATION",
                "agent_id was not issued to this team; deploy with a current city CLI",
            )

    def reserve_agent_id(self, team_id: str, family: str, agent_id: str) -> None:
        """City-run agents only: bind a chosen id to the pair. Teams use ``assign_agent_id``."""

        _require_agent(agent_id)
        self.agent_ids.reserve(team_id, family, agent_id)

    def _check_role(
        self, session: TeamSession, card: Mapping[str, Any], asked: frozenset[str]
    ) -> None:
        """ADR-044: an assigned team's role agent carries exactly its role's permissions."""

        family = family_of(card)
        role = role_permissions(family) if family is not None else frozenset[str]()
        if family is None or self.teams.assignment(session.team_id) is None:
            if asked <= session.permissions:
                return
            if family is not None and asked == role:
                raise CapError("GRANT_PERMISSION_DENIED", UNASSIGNED)
            raise CapError("GRANT_PERMISSION_DENIED", "permission")
        if asked != role:
            raise CapError(
                "GRANT_PERMISSION_DENIED",
                f"a {family} grant carries {', '.join(sorted(role))}; upgrade city-cli",
            )
        agent_id = str(card["agent_id"])
        latest: dict[str, Deployment] = {}
        for item in sorted(self._team_rows(session.team_id), key=lambda row: row.created_at):
            if item.status != "failed":
                latest[item.agent_id] = item
        for other, item in latest.items():
            if other != agent_id and family_of(item.card) == family and not self._retired(other):
                raise CapError("GRANT_PERMISSION_DENIED", f"{other} already serves {family}")

    def _retired(self, agent_id: str) -> bool:
        record = self._existing(agent_id)
        return record is not None and record.visibility == "retired"

    def _verify_only(self, session: TeamSession, grant: dict[str, Any], jws: str) -> None:
        if session.controller is None:
            raise CapError("KEY_UNKNOWN", "controller")
        verify(SignedGrant(grant=grant, jws=jws), session.controller, now=self.now())

    def _assert_quota(self, team_id: str, agent_id: str) -> None:
        assert_create(
            known_agents=self._team_agents(team_id),
            agent_id=agent_id,
            recent_deploys=len(self._recent_deploys(team_id, agent_id)),
        )

    def _team_agents(self, team_id: str) -> set[str]:
        return {item.agent_id for item in self._team_rows(team_id)}

    def _recent_deploys(self, team_id: str, agent_id: str) -> list[datetime]:
        moments = [
            item.created_at for item in self._team_rows(team_id) if item.agent_id == agent_id
        ]
        return recent(moments, self.now())

    def _owned(self, session: TeamSession, deployment_id: str, *, hold: bool = False) -> Deployment:
        found = self._deployment(deployment_id, hold=hold)
        if found is None or found.team_id != session.team_id:
            raise CapError("TARGET_NOT_VISIBLE", deployment_id)
        return found

    def _profile(self, agent_id: str, version: str) -> str:
        patch = version.rsplit(".", 1)[-1]
        if patch == "0" or self.runtime.serving(agent_id) is None:
            return "full"
        return "fast"

    async def _run_suite(self, deployment: Deployment, profile: str) -> Report:
        """Run the candidate suite. On FAIL, bounce the candidate and retry twice.

        A failed suite leaves the pod's policy epoch advanced (``alter``), so a retry
        against the same process fails ``alter: ping`` with ``STALE_EPOCH``. Re-applying
        traffic stamps a new placement and rolls fresh pods before the next attempt.
        """

        deployment.profiles.append(profile)
        report = await self._suite_once(deployment, profile)
        for attempt in range(1, 3):
            if _outcome_of(report) == "PASS":
                break
            _LOG.warning(
                "conformance failed; bouncing candidate and retrying",
                extra={
                    "agent_id": deployment.agent_id,
                    "profile": profile,
                    "attempt": attempt,
                    "detail": _suite_detail(report),
                },
            )
            await self._bounce_candidate_for_conform_retry(deployment)
            self._audit("conformance retry", deployment.agent_id)
            deployment.profiles.append(profile)
            report = await self._suite_once(deployment, profile)
        deployment.stages = [row.as_dict() for row in report.rows]
        return report

    async def _suite_once(self, deployment: Deployment, profile: str) -> Report:
        self.conform_runs += 1
        try:
            return await self.suite.run(card=deployment.card, profile=profile)
        except Exception:
            _LOG.exception(
                "conformance suite raised; retrying once",
                extra={"agent_id": deployment.agent_id, "profile": profile},
            )
            self.conform_runs += 1
            self._audit("platform retry", deployment.agent_id)
            return await self.suite.run(card=deployment.card, profile=profile)

    async def _bounce_candidate_for_conform_retry(self, deployment: Deployment) -> None:
        """Roll the candidate pods so the next suite starts at policy epoch 0."""

        if not self.runtime.remote:
            await asyncio.sleep(0)
            return
        try:
            await self._apply_traffic(deployment, 1, publish=False)
        except Exception:
            _LOG.exception(
                "candidate bounce before conformance retry failed",
                extra={"agent_id": deployment.agent_id},
            )
            wait = getattr(self.runtime, "wait_ready", None)
            if callable(wait):
                try:
                    await wait(deployment.agent_id, pods=False)
                except Exception:
                    _LOG.exception(
                        "route wait before conformance retry failed",
                        extra={"agent_id": deployment.agent_id},
                    )
            await asyncio.sleep(2.0)

    async def _apply_traffic(
        self,
        deployment: Deployment,
        percent: int,
        *,
        publish: bool = True,
        front_door: bool = False,
    ) -> None:
        # Harness join resolves the team controller via Index.get(agent_id).
        # Register before the pod starts; stay off the public query until PASS.
        await self._register_candidate(deployment)
        digest = deployment.image_digest or "sha256:pending"
        await self._offload(
            self.runtime.set_traffic,
            deployment.agent_id,
            deployment.version,
            deployment.card,
            digest,
            percent,
        )
        opener = getattr(self.runtime, "open_front_door", None)
        if front_door and callable(opener):
            await self._offload(opener, deployment.agent_id)
        self.runtime.egress[deployment.agent_id] = EGRESS
        await self._offload(self.secrets.issue_llm, deployment.agent_id)
        self._record_placement(deployment)
        wait = getattr(self.runtime, "wait_ready", None)
        if callable(wait):
            await wait(deployment.agent_id)
        if percent < 100 or not publish:
            return
        await self._publish(deployment)

    async def _promote_update(self, deployment: Deployment) -> None:
        """Retarget the serving route at the ready candidate; settle the serving host only."""

        digest = deployment.image_digest or "sha256:pending"
        promote = getattr(self.runtime, "promote_candidate", None)
        if callable(promote):
            await self._offload(
                promote,
                deployment.agent_id,
                deployment.version,
                deployment.card,
                digest,
            )
            self.runtime.egress[deployment.agent_id] = EGRESS
            self._record_placement(deployment)
            wait = getattr(self.runtime, "wait_ready", None)
            if callable(wait):
                await wait(deployment.agent_id, pods=False)
            await self._publish(deployment)
            return
        await self._apply_traffic(deployment, 100)

    async def _register_candidate(self, deployment: Deployment) -> None:
        """Stage the card so relay join can verify the grant. It is not in the catalog."""

        await self.index.stage_agent(deployment.card, role_header="deploy")

    def _confirm_released(self, deployment: Deployment) -> None:
        """ADR-065: one released agent joins the roster. A miss does not undo the release."""

        confirm = getattr(self.teams, "confirm_released", None)
        if not callable(confirm):
            return
        family = family_of(deployment.card)
        if family is None:
            return
        types = deployment.card.get("service_types")
        service_type = (
            types[0]
            if isinstance(types, list) and types and isinstance(types[0], str) and types[0]
            else None
        )
        try:
            confirm(
                team_id=deployment.team_id,
                agent_id=deployment.agent_id,
                family=family,
                service_type=service_type,
                now=self.now(),
            )
        except Exception:
            _LOG.exception(
                "roster confirm on deploy failed",
                extra={"need_id": None, "msg_id": None, "agent_id": deployment.agent_id},
            )

    async def _publish(self, deployment: Deployment) -> None:
        await self._index_publish(deployment)
        self.runtime.smoked.add(deployment.agent_id)

    def _release_gap(self, deployment: Deployment) -> str | None:
        """None when this version is serving and Index has published the card.

        ``visible`` is the public release. ``quarantined`` still counts as published
        (governance hold must survive a new promote).
        """
        record = self._existing(deployment.agent_id)
        if record is None:
            return "agent not in Index"
        if record.active_version != deployment.version:
            return f"Index active_version {record.active_version}, expected {deployment.version}"
        if record.visibility not in {"visible", "quarantined"}:
            return f"Index visibility is {record.visibility}, expected visible"
        serving = self.runtime.serving(deployment.agent_id)
        if serving is None:
            return "agent not serving"
        if serving.version != deployment.version:
            return f"serving {serving.version}, expected {deployment.version}"
        return None

    async def _pod_failed(self, deployment: Deployment, exc: RolloutFailed) -> None:
        """The pod started and then failed. Keep the exception, then delete the workload."""

        _LOG.error(
            "agent pod failed after start",
            extra={"agent_id": deployment.agent_id, "detail": exc.detail},
        )
        await self._withdraw(deployment, delete=True)

    async def _close_front_door(self, deployment: Deployment) -> None:
        closer = getattr(self.runtime, "close_front_door", None)
        if not callable(closer):
            return
        try:
            await self._offload(closer, deployment.agent_id)
        except CapError:
            _LOG.exception(
                "serving route of a failed first release not removed",
                extra={"agent_id": deployment.agent_id},
            )

    async def _withdraw(self, deployment: Deployment, *, delete: bool = False) -> None:
        """Undo a placement: no traffic, not certified, then stop or delete its pods."""

        for row in self.runtime.revisions.get(deployment.agent_id, []):
            if row.version == deployment.version:
                row.traffic = 0
                row.certified = False
        if delete:
            remove = getattr(self.runtime, "remove", None)
            if callable(remove):
                try:
                    await self._offload(remove, deployment.agent_id)
                except CapError:
                    _LOG.exception(
                        "failed to remove crashed agent",
                        extra={"agent_id": deployment.agent_id},
                    )
                return
        quiesce = getattr(self.runtime, "quiesce", None)
        if callable(quiesce):
            await self._offload(quiesce, deployment.agent_id)

    def _record_placement(self, deployment: Deployment) -> None:
        locate = getattr(self.runtime, "candidate_url", None)
        if not callable(locate):
            return
        url = locate(deployment.agent_id)
        if not isinstance(url, str):
            raise CapError("POLICY_VIOLATION", "invoke")
        self.index.catalog.put_placement(deployment.agent_id, url, deployment.version)

    async def _index_publish(self, deployment: Deployment) -> None:
        """Publish only after health. The card is visible; there is no earlier state."""

        await self.index.put_agent(deployment.card, role_header="deploy")

    async def _restore(self, revision: Revision) -> None:
        agent_id = str(revision.card["agent_id"])
        await self._offload(
            self.runtime.set_traffic,
            agent_id,
            revision.version,
            revision.card,
            revision.image_digest,
            100,
        )
        await self.index.put_agent(revision.card, role_header="deploy")
        await self._scale_down_superseded(agent_id)

    async def _scale_down_superseded(self, agent_id: str) -> None:
        """Best effort: a cluster error leaves workloads running but does not undo the release."""

        scale_down = getattr(self.runtime, "scale_down_superseded", None)
        if not callable(scale_down):
            return
        try:
            names = await scale_down(agent_id)
        except CapError:
            _LOG.exception("superseded workloads not scaled down", extra={"agent_id": agent_id})
            return
        if names:
            _LOG.info(
                "superseded workloads scaled down",
                extra={"agent_id": agent_id, "deployments": names},
            )

    async def _stop_unpassed(self, deployment: Deployment, *, direct: bool) -> None:
        """A placement that did not pass stops; scale and quiesce point back at the release."""

        if not self.runtime.remote:
            return
        if direct:
            await self._close_front_door(deployment)
            await self._withdraw(deployment, delete=True)
            return
        rows = self.runtime.revisions.get(deployment.agent_id, [])
        if any(row.version == deployment.version and row.traffic == 100 for row in rows):
            return
        await self._withdraw(deployment, delete=True)
        adopt = getattr(self.runtime, "adopt", None)
        if callable(adopt):
            await self._offload(adopt, deployment.agent_id)

    def _rollback_target(self, agent_id: str, version: str | None) -> Revision | None:
        rows = [
            row
            for row in self.runtime.revisions.get(agent_id, [])
            if row.certified and (version is None or row.version == version)
        ]
        serving = self.runtime.serving(agent_id)
        if version is None and serving is not None:
            rows = [row for row in rows if row.version != serving.version]
        if not rows:
            return None
        return rows[-1]

    def _existing(self, agent_id: str) -> AgentRecord | None:
        try:
            return self.index.catalog.get(agent_id)
        except CapError as exc:
            if exc.code == "TARGET_NOT_VISIBLE":
                return None
            raise

    def snapshot(self) -> dict[str, object]:
        with self._book_held():
            return self._snapshot()

    def _snapshot(self) -> dict[str, object]:
        return {
            "deployments": {
                dep_id: _deployment_blob(dep) for dep_id, dep in self.deployments.items()
            },
            "sessions": {token: _session_blob(session) for token, session in self.sessions.items()},
            "audit": [dict(row) for row in self.audit],
            "devices": {code: _device_blob(item) for code, item in self._devices.items()},
            "runtime": _book_blob(self.runtime),
            "secret_names": {agent: sorted(names) for agent, names in self.secret_names.items()},
            "agent_ids": self.agent_ids.blob(),
        }

    def restore(self, snapshot: Mapping[str, object]) -> None:
        if self.shared is not None:
            raw = snapshot.get("runtime")
            self._restore_book(None if raw is None else _book(raw))
            return
        deployments = _deployments(snapshot.get("deployments"))
        sessions = _sessions(snapshot.get("sessions"))
        audit = _audit(snapshot.get("audit"))
        devices = _devices(snapshot.get("devices"))
        raw_book = snapshot.get("runtime")
        book = None if raw_book is None else _book(raw_book)
        self.deployments = deployments
        self.sessions = sessions
        self.audit = audit
        self._devices = devices
        raw_names = snapshot.get("secret_names")
        self.secret_names = {
            str(agent): set(_texts(names))
            for agent, names in (raw_names.items() if isinstance(raw_names, Mapping) else [])
        }
        self.agent_ids.load(snapshot.get("agent_ids"))
        self._restore_book(book)

    def _restore_book(self, book: _Book | None) -> None:
        """The placement book stays in the snapshot until pipelines take leases (T12)."""

        _load_book(self.runtime, self._rebuild_book() if book is None else book)
        adopt = getattr(self.runtime, "adopt", None)
        if callable(adopt):
            for agent_id in list(self.runtime.revisions):
                adopt(agent_id)

    def _rebuild_book(self) -> _Book:
        """Snapshots written before the book: promoted versions, serving the one Index lists."""

        found = _Book()
        for deployment in sorted(self._all_rows(), key=lambda item: item.created_at):
            if (
                deployment.outcome != "PASS"
                or deployment.status != "promoted"
                or deployment.image_digest is None
            ):
                continue
            rows = found.revisions.setdefault(deployment.agent_id, [])
            rows[:] = [row for row in rows if row.version != deployment.version]
            rows.append(
                Revision(
                    version=deployment.version,
                    card=dict(deployment.card),
                    image_digest=deployment.image_digest,
                    traffic=0,
                )
            )
        for agent_id in list(found.revisions):
            record = self._existing(agent_id)
            rows = found.revisions[agent_id]
            active = (
                None
                if record is None
                else next((row for row in rows if row.version == record.active_version), None)
            )
            if record is None or active is None:
                del found.revisions[agent_id]
                continue
            active.traffic = 100
            found.egress[agent_id] = EGRESS
            if record.visibility == "retired":
                found.drained.add(agent_id)
            else:
                found.smoked.add(agent_id)
        return found


@dataclass
class _Book:
    revisions: dict[str, list[Revision]] = field(default_factory=dict)
    scales: dict[str, Scale] = field(default_factory=dict)
    drained: set[str] = field(default_factory=set)
    smoked: set[str] = field(default_factory=set)
    egress: dict[str, tuple[str, ...]] = field(default_factory=dict)


def _queued(deployment: Deployment, ahead: int) -> None:
    deployment.detail = f"waiting for a rollout slot, {ahead} ahead"


def _book_blob(runtime: Runtime) -> dict[str, object]:
    return {
        "revisions": {
            agent_id: [
                {
                    "version": row.version,
                    "card": row.card,
                    "image_digest": row.image_digest,
                    "traffic": row.traffic,
                    "certified": row.certified,
                }
                for row in rows
            ]
            for agent_id, rows in runtime.revisions.items()
        },
        "scales": {
            agent_id: {
                "min": scale.min_replicas,
                "max": scale.max_replicas,
                "concurrency": scale.concurrency,
            }
            for agent_id, scale in runtime.scales.items()
        },
        "drained": sorted(runtime.drained),
        "smoked": sorted(runtime.smoked),
        "egress": {agent_id: list(planes) for agent_id, planes in runtime.egress.items()},
    }


def _book(value: object) -> _Book:
    blob = _object_map(value, "runtime")
    found = _Book()
    for agent_id, rows in _object_map(blob.get("revisions"), "revisions").items():
        if not isinstance(rows, list):
            raise CapError("SCHEMA_INVALID", "revisions")
        found.revisions[agent_id] = [_revision(row) for row in rows]
    for agent_id, item in _object_map(blob.get("scales"), "scales").items():
        scale = _object_map(item, "scale")
        found.scales[agent_id] = Scale(
            _count(scale.get("min")), _count(scale.get("max")), _count(scale.get("concurrency"))
        )
    found.drained = set(_texts(blob.get("drained")))
    found.smoked = set(_texts(blob.get("smoked")))
    found.egress = {
        agent_id: tuple(_texts(planes))
        for agent_id, planes in _object_map(blob.get("egress"), "egress").items()
    }
    return found


def _revision(value: object) -> Revision:
    blob = _object_map(value, "revision")
    certified = blob.get("certified")
    if not isinstance(certified, bool):
        raise CapError("SCHEMA_INVALID", "certified")
    return Revision(
        version=_text(blob, "version"),
        card=_any_map(blob.get("card")),
        image_digest=_text(blob, "image_digest"),
        traffic=_count(blob.get("traffic")),
        certified=certified,
    )


def _load_book(runtime: Runtime, book: _Book) -> None:
    # A restore must not touch the cluster: the base methods skip the apply hooks that
    # GkeRuntime puts on scales and drained.
    runtime.revisions.clear()
    runtime.revisions.update(book.revisions)
    dict.clear(runtime.scales)
    dict.update(runtime.scales, book.scales)
    set.clear(runtime.drained)
    set.update(runtime.drained, book.drained)
    runtime.smoked.clear()
    runtime.smoked.update(book.smoked)
    runtime.egress.clear()
    runtime.egress.update(book.egress)


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise CapError("SCHEMA_INVALID", "count")
    return value


def _card(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CapError("SCHEMA_INVALID", "card")
    card = {str(key): item for key, item in value.items()}
    missing = [key for key in _CARD_FIELDS if key not in card]
    if missing:
        raise CapError("SCHEMA_INVALID", "card missing " + ",".join(missing))
    agent_id = card["agent_id"]
    team = card["team"]
    version = card["version"]
    if not isinstance(agent_id, str) or _AGENT.fullmatch(agent_id) is None:
        raise CapError("SCHEMA_INVALID", "agent_id")
    if not isinstance(team, str) or _TEAM.fullmatch(team) is None:
        raise CapError("SCHEMA_INVALID", "team")
    if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
        raise CapError("SCHEMA_INVALID", "version")
    keys = card["keys"]
    if not isinstance(keys, list) or not keys or not isinstance(keys[0], dict):
        raise CapError("SCHEMA_INVALID", "keys")
    return card


def _grant_body(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CapError("SCHEMA_INVALID", "grant")
    return {str(key): item for key, item in value.items()}


def _pinned_digest(value: str) -> str:
    return canonical_image(value)


def _grant_document(grant: Mapping[str, Any], jws: str) -> str:
    try:
        return json.dumps({"grant": grant, "jws": jws}, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise CapError("SCHEMA_INVALID", "grant") from exc


def _require_agent(agent_id: str) -> None:
    if _AGENT.fullmatch(agent_id) is None:
        raise CapError("SCHEMA_INVALID", "agent_id")


def _public_x(jwk: Mapping[str, object]) -> str:
    if any(name in jwk for name in ("d", "priv", "p", "q")):
        raise CapError("SCHEMA_INVALID", "public JWK must not carry a private field")
    return PublicKey.from_jwk(jwk).to_jwk()["x"]


def _deployments(value: object) -> dict[str, Deployment]:
    raw = _object_map(value, "deployments")
    found: dict[str, Deployment] = {}
    for dep_id, item in raw.items():
        row = found[dep_id] = deployment_from(item)
        # A pipeline lives in the process that ran it. After a restart it is gone.
        if row.outcome is None and row.status in {"building", "conformance"}:
            interrupted(row)
    return found


def interrupted(row: Deployment) -> None:
    row.status = "failed"
    row.outcome = "ERROR"
    row.step = None
    row.detail = "deploy restarted during the pipeline; run city deploy again"


def _sessions(value: object) -> dict[str, TeamSession]:
    raw = _object_map(value, "sessions")
    return {token: _session(item) for token, item in raw.items()}


def _devices(value: object) -> dict[str, DeviceChallenge]:
    raw = _object_map(value, "devices")
    return {code: device_from(item) for code, item in raw.items()}


def _audit(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise CapError("SCHEMA_INVALID", "audit")
    rows: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise CapError("SCHEMA_INVALID", "audit")
        row: dict[str, str] = {}
        for key, cell in item.items():
            if not isinstance(key, str) or not isinstance(cell, str):
                raise CapError("SCHEMA_INVALID", "audit")
            row[key] = cell
        rows.append(row)
    return rows


def merged_stage_rows(history: Sequence[Sequence[Mapping[str, object]]]) -> list[dict[str, object]]:
    """Newest outcome per stage name. A later fast run does not erase stages it skipped."""

    by_name: dict[str, dict[str, object]] = {}
    order: list[str] = []
    for stages in history:
        if not isinstance(stages, Sequence) or isinstance(stages, (str, bytes)):
            continue
        for stage in stages:
            if not isinstance(stage, Mapping):
                continue
            name = stage.get("name")
            if not isinstance(name, str) or not name:
                continue
            if name not in by_name:
                order.append(name)
            by_name[name] = dict(stage)
    return [by_name[name] for name in order]


def _sent_count(snapshot: Mapping[str, object]) -> int:
    sent = snapshot.get("sent")
    if not isinstance(sent, list):
        return 0
    return len(sent)


def _evidence_base(runtime: object, agent_id: str) -> str:
    locate = getattr(runtime, "evidence_url", None)
    if callable(locate):
        try:
            found = str(locate(agent_id)).rstrip("/")
        except (TypeError, ValueError, CapError):
            found = ""
        if found:
            return found
    return os.environ.get("CITY_ATTESTATION_URL", "").strip().rstrip("/")


def _token_eq(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode(), right.encode())


def _pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii", errors="replace")).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")
