"""Team registry lookup for device-login approval and ASST assignment.

City Hall (ADR-027) owns registered teams. Deploy refuses to bind a device
challenge to a slug that is not in that registry. City Hall stores a team's
ASST assignment and confirmed roster on the same record (ADR-044). A release
confirms that one agent (ADR-065).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Set
from datetime import datetime
from typing import Any, Protocol, cast
from uuid import uuid4

from cap.core.errors import CapError
from cap.core.timeutil import format_time
from opentelemetry import trace

log = logging.getLogger("deploy")
tracer = trace.get_tracer("city.deploy")
_AUTO_REASON = "automatic on deploy"


class TeamDirectory(Protocol):
    def require(self, team_id: str) -> None:
        """Raise ``TARGET_NOT_VISIBLE`` when ``team_id`` is not registered."""

    def assignment(self, team_id: str) -> Mapping[str, Any] | None:
        """The team's ASST assignment, or None when it has none."""

    def assignments(self) -> list[tuple[str, Mapping[str, Any]]]:
        """Every assigned team with its assignment, in team order."""


class OpenTeamDirectory:
    """Allow any well-formed slug (local / tests / in-process CLI)."""

    def require(self, team_id: str) -> None:
        del team_id

    def assignment(self, team_id: str) -> Mapping[str, Any] | None:
        del team_id
        return None

    def assignments(self) -> list[tuple[str, Mapping[str, Any]]]:
        return []


class RegisteredTeamDirectory:
    """In-memory allow-list used by unit tests."""

    def __init__(
        self,
        team_ids: Set[str],
        assignments: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self._ids = frozenset(team_ids)
        self._assignments = dict(assignments or {})

    def require(self, team_id: str) -> None:
        if team_id not in self._ids:
            raise CapError("TARGET_NOT_VISIBLE", team_id)

    def assignment(self, team_id: str) -> Mapping[str, Any] | None:
        return self._assignments.get(team_id)

    def assignments(self) -> list[tuple[str, Mapping[str, Any]]]:
        return sorted(self._assignments.items())


class FirestoreTeamDirectory:
    """Read ``teams/{slug}`` from the Console document store (Firestore).

    A release also writes that team's roster (ADR-065). Reads stay unlocked.
    """

    def __init__(
        self,
        get_document: Callable[[str], Mapping[str, object] | None],
        *,
        store: _RosterStore | None = None,
    ) -> None:
        self._get = get_document
        self._store = store

    def confirm_released(
        self,
        *,
        team_id: str,
        agent_id: str,
        family: str,
        service_type: str | None,
        now: datetime,
    ) -> bool:
        """Confirm one released agent. False when the roster must stay as it is."""

        store = self._store
        if store is None:
            return False
        with tracer.start_as_current_span(
            "deploy.roster.confirm",
            attributes={"team_id": team_id, "agent_id": agent_id},
        ):
            wrote = store.transact(
                lambda txn: _confirm(
                    txn,
                    team_id=team_id,
                    agent_id=agent_id,
                    family=family,
                    service_type=service_type,
                    now=now,
                )
            )
        if wrote:
            log.info(
                "roster.confirm",
                extra={
                    "need_id": None,
                    "msg_id": None,
                    "agent_id": agent_id,
                    "team_id": team_id,
                },
            )
        return wrote

    def read_document(self, path: str) -> Mapping[str, object] | None:
        return self._get(path)

    def drop_agent(self, agent_id: str, *, team_id: str) -> bool:
        """Take one agent off the confirmed roster. The family is missing until the team releases
        another (ADR-093)."""

        store = self._store
        if store is None or not team_id:
            return False
        return store.transact(lambda txn: _drop(txn, team_id=team_id, agent_id=agent_id))

    def require(self, team_id: str) -> None:
        found = self._get(f"teams/{team_id}")
        if found is None or not found.get("team_id"):
            raise CapError("TARGET_NOT_VISIBLE", team_id)

    def assignment(self, team_id: str) -> Mapping[str, Any] | None:
        found = self._get(f"teams/{team_id}")
        if found is None or not found.get("team_id"):
            return None
        assigned = found.get("assignment")
        if not isinstance(assigned, Mapping) or not isinstance(assigned.get("asst"), str):
            return None
        return assigned

    def assignments(self) -> list[tuple[str, Mapping[str, Any]]]:
        index = self._get("indexes/teams")
        ids = index.get("ids") if index is not None else None
        if not isinstance(ids, list):
            return []
        rows: list[tuple[str, Mapping[str, Any]]] = []
        for team_id in sorted(item for item in ids if isinstance(item, str)):
            assigned = self.assignment(team_id)
            if assigned is not None:
                rows.append((team_id, assigned))
        return rows


class DocumentStore(Protocol):
    def read(self, fn: Callable[[Any], Any]) -> Any: ...


class _Txn(Protocol):
    def get(self, path: str) -> Mapping[str, Any] | None: ...

    def set(self, path: str, document: Mapping[str, Any]) -> None: ...


class _RosterStore(Protocol):
    def transact(self, fn: Callable[[_Txn], bool]) -> bool: ...


def firestore_team_directory(store: DocumentStore) -> FirestoreTeamDirectory:
    """Directory over the store City Hall writes ``teams/{slug}`` through.

    Reads take no lock. A release confirms its agent in one transaction on the same store.
    """

    def get_document(path: str) -> Mapping[str, object] | None:
        def read(txn: object) -> Mapping[str, object] | None:
            get = getattr(txn, "get")
            found = get(path)
            return found if isinstance(found, dict) else None

        found: Mapping[str, object] | None = store.read(read)
        return found

    writer = cast(_RosterStore, store) if callable(getattr(store, "transact", None)) else None
    return FirestoreTeamDirectory(get_document, store=writer)


def _confirm(
    txn: _Txn,
    *,
    team_id: str,
    agent_id: str,
    family: str,
    service_type: str | None,
    now: datetime,
) -> bool:
    from city_deploy.rosters import roster_after_release

    current = txn.get(f"teams/{team_id}")
    if current is None or not current.get("team_id"):
        return False
    held = current.get("assignment")
    assignment = held if isinstance(held, Mapping) else None
    agents = roster_after_release(
        assignment,
        team_id=team_id,
        family=family,
        agent_id=agent_id,
        service_type=service_type,
    )
    if agents is None or not isinstance(assignment, Mapping):
        return False
    if assignment.get("confirmed") is True and agents == _stored_agents(assignment):
        return False
    updated = dict(assignment)
    updated["confirmed"] = True
    updated["confirmed_at"] = format_time(now)
    updated["confirmed_by"] = f"team:{team_id}"
    updated["agents"] = agents
    document = dict(current)
    document["assignment"] = updated
    txn.set(f"teams/{team_id}", document)
    _audit(txn, team_id=team_id, now=now)
    return True


def _drop(txn: _Txn, *, team_id: str, agent_id: str) -> bool:
    current = txn.get(f"teams/{team_id}")
    if current is None or not current.get("team_id"):
        return False
    held = current.get("assignment")
    if not isinstance(held, Mapping):
        return False
    agents = [row for row in _stored_agents(held) if row.get("agent_id") != agent_id]
    if agents == _stored_agents(held):
        return False
    updated = dict(held)
    updated["agents"] = agents
    document = dict(current)
    document["assignment"] = updated
    txn.set(f"teams/{team_id}", document)
    return True


def _stored_agents(assignment: Mapping[str, Any]) -> list[dict[str, str]]:
    raw = assignment.get("agents")
    rows: list[dict[str, str]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, Mapping) or not isinstance(item.get("family"), str):
            continue
        row = {"family": str(item["family"]), "agent_id": str(item["agent_id"])}
        stored = item.get("service_type")
        if isinstance(stored, str) and stored:
            row["service_type"] = stored
        rows.append(row)
    return rows


def _audit(txn: _Txn, *, team_id: str, now: datetime) -> None:
    record: dict[str, Any] = {
        "record_kind": "event",
        "type": "ui.action",
        "record_id": "ui-" + uuid4().hex,
        "observer": "city:ui",
        "actor": f"team:{team_id}",
        "role": "team",
        "action": "roster.confirm",
        "reason": _AUTO_REASON,
        "target": team_id,
        "time": format_time(now),
    }
    current = txn.get("audit/log")
    items: list[dict[str, Any]] = []
    if isinstance(current, Mapping) and isinstance(current.get("items"), list):
        items = [item for item in current["items"] if isinstance(item, dict)]
    items.append(record)
    txn.set("audit/log", {"items": items})


def wave_blocks_delete(document: Mapping[str, Any] | None) -> bool:
    """An open or paused wave refuses deletion. A missing or closed wave does not."""

    if not document:
        return False
    wave = document.get("wave")
    if not isinstance(wave, Mapping):
        return False
    if wave.get("paused_at"):
        return True
    phase = wave.get("phase")
    return isinstance(phase, str) and phase not in {"", "closed"}


def market_lock_for(directory: TeamDirectory) -> Callable[[], bool] | None:
    """Cloud Deploy reads ``director/state``. A read error fails closed."""

    reader = getattr(directory, "read_document", None)
    if not callable(reader):
        return None

    def locked() -> bool:
        try:
            document = reader("director/state")
        except Exception:
            return True
        return wave_blocks_delete(document if isinstance(document, Mapping) else None)

    return locked


def open_firestore_team_directory(
    *,
    project: str,
    database: str = "(default)",
) -> FirestoreTeamDirectory:
    """Build the directory City Hall writes: Firestore, or Cloud SQL when
    ``CITY_DIRECTORY_STORE=sql`` (cloud Deploy only)."""
    import os

    import httpx
    from city_adapters.directory_store import open_directory_store
    from city_adapters.http import HttpxTransport, gcp_token_source

    tokens = gcp_token_source()
    transport = HttpxTransport(httpx.Client(timeout=15.0), tokens)
    return firestore_team_directory(
        open_directory_store(
            env=os.environ, transport=transport, tokens=tokens, project=project, database=database
        )
    )
