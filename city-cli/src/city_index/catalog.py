"""In-memory catalog snapshot with listener hooks."""

from __future__ import annotations

import inspect
import ipaddress
import re
import threading
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from cap.core.errors import CapError
from cap.core.jcs import card_digest
from cap.core.keys import PublicKey

from city_index.events import visibility_changed
from city_index.families import card_capabilities
from city_index.model import AgentRecord, Placement, VisibilityLogEntry
from city_index.visibility import assert_transition

Listener = Callable[[dict[str, Any]], Awaitable[None] | None]
_AGENT = re.compile(r"^agent:[a-z0-9_-]+(?:/[a-z0-9_-]+)?$")
_TEAM = re.compile(r"^team:[a-z0-9_-]+$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_HOST = re.compile(r"^[a-z0-9]([a-z0-9_-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9_-]{0,61}[a-z0-9])?)+$")
_PRIVATE = ("d", "priv", "p", "q")
# One key per machine. A roster of teammates, plus a rotation that has not
# finished, stays well under this. Past it, registration is refused.
MAX_CONTROLLER_KEYS = 32


def assert_complete_card(card: Mapping[str, object]) -> None:
    """Validate required CAP card fields at the untrusted HTTP boundary."""

    if not isinstance(card.get("name"), str) or not str(card["name"]).strip():
        raise CapError("SCHEMA_INVALID", "card.name required")
    cap_versions = card.get("cap_versions")
    if not isinstance(cap_versions, list) or not cap_versions or set(cap_versions) != {"1.0"}:
        raise CapError("SCHEMA_INVALID", "card.cap_versions")
    profiles = card.get("profiles")
    if (
        not isinstance(profiles, list)
        or not profiles
        or not all(isinstance(p, str) for p in profiles)
    ):
        raise CapError("SCHEMA_INVALID", "card.profiles")
    endpoints = card.get("endpoints")
    if not isinstance(endpoints, dict) or not isinstance(endpoints.get("mailbox"), str):
        raise CapError("SCHEMA_INVALID", "card.endpoints.mailbox")
    keys = card.get("keys")
    if not isinstance(keys, list) or not keys:
        raise CapError("SCHEMA_INVALID", "card.keys")


def assert_city_invoke(url: str) -> str:
    """HTTPS name the city assigned. Public IPs and payload URLs are refused."""

    parsed = urlsplit(url.strip())
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.port not in (None, 443)
        or not host
        or _HOST.fullmatch(host) is None
        or _is_ip(host)
        or host.endswith(".svc.cluster.local")
        or host.endswith(".pod.cluster.local")
        or ".." in parsed.path
        or "\\" in url
    ):
        raise CapError("POLICY_VIOLATION", "invoke")
    path = parsed.path.rstrip("/")
    return f"https://{host}{path}"


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def accept_controller_jwk(team_id: str, jwk: Mapping[str, object]) -> dict[str, str]:
    """Canonical public JWK. Rejects a private field and a bad team id."""

    if _TEAM.fullmatch(team_id) is None:
        raise CapError("SCHEMA_INVALID", "team")
    if any(name in jwk for name in _PRIVATE):
        raise CapError("SCHEMA_INVALID", "public JWK must not carry a private field")
    return dict(PublicKey.from_jwk(jwk).to_jwk())


def merge_controller_keys(
    existing: list[dict[str, str]],
    jwk: Mapping[str, str],
    retire_x: str | None,
) -> list[dict[str, str]]:
    """Add ``jwk`` and drop ``retire_x``. The same ``x`` is not stored twice."""

    keys = [dict(item) for item in existing if retire_x is None or item.get("x") != retire_x]
    nxt = dict(jwk)
    if any(item.get("x") == nxt.get("x") for item in keys):
        return keys
    if len(keys) >= MAX_CONTROLLER_KEYS:
        raise CapError("POLICY_VIOLATION", "controller keys")
    keys.append(nxt)
    return keys


class Catalog:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._agents: dict[str, AgentRecord] = {}
        self._staged: dict[str, AgentRecord] = {}
        self._log: list[VisibilityLogEntry] = []
        self._listeners: list[Listener] = []
        self._controllers: dict[str, list[dict[str, str]]] = {}
        self._placements: dict[str, Placement] = {}
        self._outbox: dict[str, dict[str, Any]] = {}
        self._citizen_grants: dict[str, dict[str, Any]] = {}
        self._generation = 0

    def refresh_snapshot(self, *, force: bool = False) -> None:
        """Refresh the query snapshot. Memory catalogs are already authoritative."""

        del force

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def refresh_resolution(self, agent_id: str | None = None) -> None:
        """Memory has no remote snapshot. Firestore reloads before a relay read."""

        del agent_id

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    async def _notify(self, event: dict[str, Any]) -> None:
        for listener in list(self._listeners):
            result = listener(event)
            if inspect.isawaitable(result):
                await result

    def get(self, agent_id: str) -> AgentRecord:
        with self._lock:
            try:
                return self._agents[agent_id]
            except KeyError as exc:
                raise CapError("TARGET_NOT_VISIBLE", agent_id) from exc

    def resolve(self, agent_id: str) -> AgentRecord:
        """Published card, or the private staged card the Relay uses to check a grant."""

        with self._lock:
            found = self._agents.get(agent_id)
            if found is None:
                found = self._staged.get(agent_id)
            if found is None:
                raise CapError("TARGET_NOT_VISIBLE", agent_id)
            return found

    def staged(self, agent_id: str) -> AgentRecord | None:
        with self._lock:
            found = self._staged.get(agent_id)
            return None if found is None else found

    def all_agents(self) -> list[AgentRecord]:
        with self._lock:
            return list(self._agents.values())

    def key_for_kid(self, kid: str) -> dict[str, str]:
        with self._lock:
            for agent in self._agents.values():
                if kid in agent.keys:
                    return dict(agent.keys[kid])
        raise CapError("KEY_UNKNOWN", kid)

    def key_for_visible_kid(self, kid: str) -> dict[str, str]:
        with self._lock:
            for agent in self._agents.values():
                if agent.visibility != "visible":
                    continue
                if kid in agent.keys:
                    return dict(agent.keys[kid])
        raise CapError("KEY_UNKNOWN", kid)

    def put_controller(
        self,
        team_id: str,
        jwk: Mapping[str, object],
        *,
        retire_x: str | None = None,
    ) -> dict[str, str]:
        """Remember ``jwk`` for ``team_id``. A second teammate's key is kept.

        ``retire_x`` drops that one public key (a local ``city keys init``).
        Other teammates' keys stay. Registering the same ``x`` again is a no-op.
        """

        stored = accept_controller_jwk(team_id, jwk)
        with self._lock:
            existing = list(self._controllers.get(team_id, []))
            self._controllers[team_id] = merge_controller_keys(existing, stored, retire_x)
        return dict(stored)

    def controller_jwks(self, team_id: str) -> list[dict[str, str]]:
        """Every controller public key registered for ``team_id``, oldest first."""

        with self._lock:
            try:
                found = self._controllers[team_id]
            except KeyError as exc:
                raise CapError("KEY_UNKNOWN", f"no controller for {team_id}") from exc
            return [dict(item) for item in found]

    def controller_jwk(self, team_id: str) -> dict[str, str]:
        """The oldest controller key. Grant checks use ``controller_jwks``."""

        return dict(self.controller_jwks(team_id)[0])

    def put_citizen_grants(self, grants: list[dict[str, Any]]) -> None:
        """Store Deploy-issued citizen grants (ADR-039). A re-issue replaces the grant."""

        with self._lock:
            for grant in grants:
                self._citizen_grants[str(grant["citizen_id"])] = dict(grant)

    def citizen_grant(self, citizen_id: str) -> dict[str, Any] | None:
        with self._lock:
            grant = self._citizen_grants.get(citizen_id)
            return None if grant is None else dict(grant)

    def put_placement(self, agent_id: str, invoke_base_url: str, revision_tag: str) -> Placement:
        if _AGENT.fullmatch(agent_id) is None:
            raise CapError("SCHEMA_INVALID", "agent_id")
        if not revision_tag or any(char.isspace() for char in revision_tag):
            raise CapError("SCHEMA_INVALID", "revision")
        row = Placement(assert_city_invoke(invoke_base_url), revision_tag)
        self._placements[agent_id] = row
        return row

    def placement(self, agent_id: str) -> Placement:
        try:
            return self._placements[agent_id]
        except KeyError as exc:
            raise CapError("TARGET_NOT_VISIBLE", agent_id) from exc

    def put_agent(
        self,
        card: dict[str, Any],
        *,
        event: dict[str, Any] | None = None,
        publish: bool = True,
    ) -> AgentRecord:
        agent_id = card.get("agent_id")
        team = card.get("team")
        version = card.get("version")
        if (
            not isinstance(agent_id, str)
            or _AGENT.fullmatch(agent_id) is None
            or not isinstance(team, str)
            or _TEAM.fullmatch(team) is None
        ):
            raise CapError("SCHEMA_INVALID", "card.agent_id and card.team required")
        if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
            raise CapError("SCHEMA_INVALID", "card.version required")
        cap_versions = card.get("cap_versions")
        if cap_versions is not None and (
            not isinstance(cap_versions, list) or not cap_versions or set(cap_versions) != {"1.0"}
        ):
            raise CapError("SCHEMA_INVALID", "card.cap_versions")
        if "profiles" in card and (
            not isinstance(card.get("profiles"), list) or not card["profiles"]
        ):
            raise CapError("SCHEMA_INVALID", "card.profiles")
        endpoints = card.get("endpoints")
        if endpoints is not None and (
            not isinstance(endpoints, dict)
            or ("mailbox" in endpoints and not isinstance(endpoints.get("mailbox"), str))
        ):
            raise CapError("SCHEMA_INVALID", "card.endpoints.mailbox")
        digest = card_digest(card)
        keys: dict[str, dict[str, str]] = {}
        raw_keys = card.get("keys")
        if raw_keys is not None and (not isinstance(raw_keys, list) or not raw_keys):
            raise CapError("SCHEMA_INVALID", "card.keys")
        for item in raw_keys if isinstance(raw_keys, list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("kid"), str):
                raise CapError("SCHEMA_INVALID", "card.keys")
            kid = str(item["kid"])
            if not kid.startswith(agent_id + "#"):
                raise CapError("SCHEMA_INVALID", "card.keys.kid")
            public = PublicKey.from_jwk(item).to_jwk()
            keys[kid] = dict(public)
        profiles = (
            [str(p) for p in card.get("profiles", [])]
            if isinstance(card.get("profiles"), list)
            else []
        )
        with self._lock:
            existing = self._agents.get(agent_id)
            owner = existing if existing is not None else self._staged.get(agent_id)
            if owner is not None and owner.team_id != team:
                raise CapError("DUPLICATE", "agent_id belongs to another team")
            # A publish is a ready release. A hold does not survive a version that passed health.
            visibility = "visible"
            record = AgentRecord(
                agent_id=agent_id,
                team_id=team,
                visibility=visibility,
                card=dict(card),
                card_digest=digest,
                active_version=version,
                rating_count=existing.rating_count if existing is not None else 0,
                role=str(card["role"]) if isinstance(card.get("role"), str) else None,
                capabilities=card_capabilities(card),
                districts=_int_list(card.get("districts")),
                languages=_str_list(card.get("languages")),
                accessibility=_str_list(card.get("accessibility")),
                profiles=profiles,
                keys=keys,
                quarantine_until=existing.quarantine_until if existing is not None else None,
                created_at=(
                    existing.created_at
                    if existing is not None and existing.created_at
                    else event.get("time")
                    if isinstance(event, dict) and isinstance(event.get("time"), str)
                    else None
                ),
                unverified=existing.unverified if existing is not None else False,
            )
            if isinstance(event, dict) and isinstance(event.get("time"), str):
                record.promoted_at = str(event["time"])
            elif existing is not None:
                record.promoted_at = existing.promoted_at
            if publish:
                self._staged.pop(agent_id, None)
                self._agents[agent_id] = record
                if event is not None:
                    self._store_outbox(event)
                if existing is None and event is not None:
                    at = event.get("time")
                    if isinstance(at, str):
                        self._log.append(
                            VisibilityLogEntry(
                                agent_id=agent_id,
                                from_state="absent",
                                to_state="visible",
                                cause="deploy",
                                observer=str(event.get("observer") or "city:deploy"),
                                at=at,
                            )
                        )
                        self._store_outbox(
                            visibility_changed(
                                agent_id=agent_id,
                                from_state="absent",
                                to_state="visible",
                                cause="deploy",
                                observer=str(event.get("observer") or "city:deploy"),
                                at=at,
                            )
                        )
            else:
                self._staged[agent_id] = record
            self._generation += 1
        return record

    def mark_unverified(self, agent_id: str) -> AgentRecord:
        with self._lock:
            agent = self.get(agent_id)
            agent.unverified = True
            self._generation += 1
            return agent

    def drop_stage(self, agent_id: str) -> None:
        with self._lock:
            self._staged.pop(agent_id, None)

    def delete_agent(self, agent_id: str, *, event: dict[str, Any] | None = None) -> bool:
        """Drop the published card, the staged card, and the placement. True when one existed."""

        with self._lock:
            published = self._agents.pop(agent_id, None)
            staged = self._staged.pop(agent_id, None)
            self._placements.pop(agent_id, None)
            if published is None and staged is None:
                return False
            if event is not None:
                self._store_outbox(event)
            self._generation += 1
            return True

    async def set_visibility(
        self,
        agent_id: str,
        nxt: str,
        *,
        cause: str,
        observer: str,
        at: str,
        event: dict[str, Any] | None = None,
    ) -> AgentRecord:
        with self._lock:
            agent = self.get(agent_id)
            previous = agent.visibility
            assert_transition(previous, nxt)
            agent.visibility = nxt
            self._log.append(
                VisibilityLogEntry(
                    agent_id=agent_id,
                    from_state=previous,
                    to_state=nxt,
                    cause=cause,
                    observer=observer,
                    at=at,
                )
            )
            if event is not None:
                self._store_outbox(event)
            self._generation += 1
        event = {
            "type": "visibility.changed",
            "record_id": visibility_changed(
                agent_id=agent_id,
                from_state=previous,
                to_state=nxt,
                cause=cause,
                observer=observer,
                at=at,
            )["record_id"],
            "agent_id": agent_id,
            "from": previous,
            "to": nxt,
            "cause": cause,
            "observer": observer,
            "at": at,
        }
        await self._notify(event)
        return agent

    @property
    def visibility_log(self) -> list[VisibilityLogEntry]:
        with self._lock:
            return list(self._log)

    def _store_outbox(self, event: dict[str, Any]) -> None:
        record_id = event.get("record_id")
        if not isinstance(record_id, str) or not record_id:
            raise CapError("SCHEMA_INVALID", "event.record_id")
        self._outbox[record_id] = dict(event)

    def pending_events(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(event) for _, event in sorted(self._outbox.items())]

    def mark_event_published(self, record_id: str) -> None:
        with self._lock:
            self._outbox.pop(record_id, None)


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _int_list(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    out: list[int] = []
    for item in value:
        if isinstance(item, int):
            out.append(item)
    return out
