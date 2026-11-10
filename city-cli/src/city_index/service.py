"""Index application service."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from cap.core.errors import CapError
from cap.core.timeutil import format_time

from city_index import events
from city_index.auth import DEPLOY, PROBER, TEAM, require_role
from city_index.cache import QueryCache, QueryCachePort
from city_index.catalog import Catalog
from city_index.citizens import CitizenKeys, is_citizen
from city_index.families import FAMILIES, family_of
from city_index.ports import Clock, EventPublisher, SystemClock
from city_index.query import cache_key, latest_per_team, matches, shuffle, summary
from city_index.reputation import NoReputation, ReputationPort
from city_index.search import Resolver, families_for, ledger_row
from city_index.visibility import TEAM_RESTORE
from city_index.watch import WatchHub

log = logging.getLogger("city.index")


@dataclass
class MemoryPublisher:
    records: list[dict[str, Any]] = field(default_factory=list)

    async def publish(self, record: Mapping[str, object]) -> None:
        self.records.append(dict(record))


@dataclass
class FixedClock:
    moment: datetime

    def now(self) -> datetime:
        return self.moment


class IndexService:
    def __init__(
        self,
        *,
        catalog: Catalog | None = None,
        cache: QueryCachePort | None = None,
        clock: Clock | None = None,
        publisher: EventPublisher | None = None,
        watch: WatchHub | None = None,
        reputation: ReputationPort | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self.catalog = catalog or Catalog()
        self.resolver = resolver
        self.reputation: ReputationPort = reputation or NoReputation()
        self.cache: QueryCachePort = cache if cache is not None else QueryCache()
        self.clock = clock or SystemClock()
        self.publisher = publisher or MemoryPublisher()
        self.watch = watch or WatchHub()
        self.citizens = CitizenKeys(self.catalog, self.clock)
        self._catalog_generation = self.catalog.generation
        self._watch_state = self._current_watch_state()
        self.catalog.subscribe(self._on_catalog_event)

    async def _on_catalog_event(self, event: dict[str, Any]) -> None:
        self.cache.invalidate()
        await self.watch.publish(event)

    def _minute_bucket(self) -> str:
        now = self.clock.now().astimezone(UTC)
        return now.strftime("%Y-%m-%dT%H:%M")

    def _refresh(self, *, force: bool = False) -> None:
        self.catalog.refresh_snapshot(force=force)
        generation = self.catalog.generation
        if generation != self._catalog_generation:
            self.cache.invalidate()
            self._catalog_generation = generation

    def refresh_catalog(self, *, force: bool = True) -> None:
        """Reload the catalog from Firestore (no-op for an in-memory catalog).

        Console readers that call ``catalog.all_agents()`` must use this first;
        otherwise a delete performed by Deploy or another replica stays invisible
        until the process restarts.
        """

        self._refresh(force=force)

    def _current_watch_state(self) -> dict[str, tuple[str, str, str]]:
        return {
            agent.agent_id: (agent.visibility, agent.active_version, agent.card_digest)
            for agent in self.catalog.all_agents()
        }

    def _remember_watch_state(self) -> None:
        self._watch_state = self._current_watch_state()

    async def sync_watch_once(self) -> None:
        """Refresh Firestore and fan out changes made by another service or replica."""

        await asyncio.to_thread(self._refresh, force=True)
        current = self._current_watch_state()
        previous = self._watch_state
        self._watch_state = current
        for agent_id, (visibility, version, digest) in current.items():
            old = previous.get(agent_id)
            if old is None or old[1:] != (version, digest):
                stable = hashlib.sha256(f"{agent_id}\0{version}\0{digest}".encode()).hexdigest()
                await self.watch.publish(
                    {
                        "type": "agent.promoted",
                        "record_id": f"snapshot:{stable}",
                        "agent_id": agent_id,
                        "version": version,
                    }
                )
            if old is not None and old[0] != visibility:
                stable = hashlib.sha256(
                    f"{agent_id}\0{old[0]}\0{visibility}\0{digest}".encode()
                ).hexdigest()
                await self.watch.publish(
                    {
                        "type": "visibility.changed",
                        "record_id": f"snapshot:{stable}",
                        "agent_id": agent_id,
                        "from": old[0],
                        "to": visibility,
                        "cause": "snapshot-refresh",
                    }
                )

    async def drain_outbox_once(self) -> int:
        published = 0
        for event in self.catalog.pending_events():
            record_id = event.get("record_id")
            if not isinstance(record_id, str):
                continue
            try:
                await self.publisher.publish(event)
                self.catalog.mark_event_published(record_id)
                published += 1
            except Exception:
                log.exception("index event publish failed", extra={"record_id": record_id})
                break
        return published

    def ready(self) -> dict[str, object]:
        self._refresh(force=True)
        self.cache.get("__index_readiness__", now=self.clock.now())
        return {
            "status": "ready",
            "catalog_generation": self.catalog.generation,
            "agents": len(self.catalog.all_agents()),
        }

    async def put_agent(self, card: dict[str, Any], *, role_header: str | None) -> dict[str, Any]:
        require_role(role_header, DEPLOY)
        self._refresh(force=True)
        at = format_time(self.clock.now())
        event = events.agent_promoted(
            agent_id=str(card.get("agent_id", "")),
            version=str(card.get("version", "")),
            team=str(card.get("team", "")).removeprefix("team:"),
            family=family_of(card),
            observer="city:deploy",
            at=at,
        )
        record = self.catalog.put_agent(card, event=event)
        if record.created_at is None:
            record.created_at = at
        self.cache.invalidate()
        await self.drain_outbox_once()
        await self.watch.publish(
            {
                "type": "agent.promoted",
                "record_id": event["record_id"],
                "agent_id": record.agent_id,
                "version": record.active_version,
                "at": at,
            }
        )
        self._remember_watch_state()
        return self.agent_record(record.agent_id)

    async def stage_agent(self, card: dict[str, Any], *, role_header: str | None) -> None:
        """Private card for a candidate. Not queried, not watched, not in the catalog."""

        require_role(role_header, DEPLOY)
        self._refresh(force=True)
        self.catalog.put_agent(card, publish=False)

    async def delete_agent(
        self, agent_id: str, *, role_header: str | None, cause: str = "delete"
    ) -> bool:
        require_role(role_header, DEPLOY)
        self._refresh(force=True)
        at = format_time(self.clock.now())
        event = events.agent_deleted(agent_id=agent_id, cause=cause, observer="city:deploy", at=at)
        removed = self.catalog.delete_agent(agent_id, event=event)
        self.cache.invalidate()
        if removed:
            await self.drain_outbox_once()
        return removed

    async def put_unverified(
        self, card: dict[str, Any], *, role_header: str | None
    ) -> dict[str, Any]:
        """City impostor. Present for include=unverified, absent from discovery."""

        record = await self.put_agent(card, role_header=role_header)
        self.catalog.mark_unverified(str(record["agent_id"]))
        self.cache.invalidate()
        return self.agent_record(str(record["agent_id"]))

    async def patch_visibility(
        self,
        agent_id: str,
        visibility: str,
        *,
        role_header: str | None,
        cause: str = "order",
    ) -> dict[str, Any]:
        observer = require_role(role_header, PROBER, TEAM)
        if observer == TEAM:
            current = self.catalog.get(agent_id).visibility
            if visibility != "visible" or current not in TEAM_RESTORE:
                raise CapError("POLICY_VIOLATION", "team")
        self._refresh(force=True)
        at = format_time(self.clock.now())
        previous = self.catalog.get(agent_id).visibility
        event = events.visibility_changed(
            agent_id=agent_id,
            from_state=previous,
            to_state=visibility,
            cause=cause,
            observer=f"city:{observer}",
            at=at,
        )
        try:
            await self.catalog.set_visibility(
                agent_id,
                visibility,
                cause=cause,
                observer=f"city:{observer}",
                at=at,
                event=event,
            )
        except CapError as exc:
            if exc.code == "POLICY_VIOLATION":
                # OpenAPI index.yaml maps disallowed transitions to 409.
                raise TransitionConflict(exc.message) from exc
            raise
        await self.drain_outbox_once()
        self._remember_watch_state()
        return self.agent_record(agent_id)

    def agent_record(self, agent_id: str) -> dict[str, Any]:
        self._refresh()
        agent = self.catalog.get(agent_id)
        return {
            "agent_id": agent.agent_id,
            "visibility": agent.visibility,
            "card_digest": agent.card_digest,
            "version": agent.active_version,
            "rating_count": agent.rating_count,
        }

    def pinned_card(self, agent_id: str) -> dict[str, Any]:
        self._refresh()
        return dict(self.catalog.get(agent_id).card)

    def get_key(self, kid: str) -> dict[str, str]:
        if is_citizen(kid.partition("#")[0]):
            try:
                return self.citizens.key(kid, "invoke").to_jwk()
            except KeyError as exc:
                raise CapError("KEY_UNKNOWN", kid) from exc
        self._refresh()
        return self.catalog.key_for_kid(kid)

    def query(self, *, caller: str, role: str, include: str | None = None) -> dict[str, Any]:
        """Each team's visible agent of ``role``, with its reputation, in the shuffle."""

        if role not in FAMILIES:
            raise CapError("SCHEMA_INVALID", "role")
        if include not in (None, "unverified"):
            raise CapError("SCHEMA_INVALID", "include")
        self._refresh()
        include_unverified = include == "unverified"
        now = self.clock.now()
        minute = self._minute_bucket()
        key = cache_key(
            role=role,
            include_unverified=include_unverified,
            caller=caller,
            minute_bucket=minute,
        )
        cached = self.cache.get(key, now=now)
        if cached is not None:
            return {"agents": cached}
        matched = [
            agent
            for agent in self.catalog.all_agents()
            if matches(agent, role=role, include_unverified=include_unverified)
        ]
        ordered = shuffle(latest_per_team(matched), caller=caller, minute_bucket=minute)
        scores = self.reputation.scores(now)
        payload = [
            summary(agent, role=role, reputation=scores.get(agent.agent_id, (0, 0))[0])
            for agent in ordered
        ]
        self.cache.put(key, payload, now=now)
        return {"agents": payload}

    def search(self, *, caller: str, query: object) -> dict[str, Any]:
        """ADR-110: the families the text names, and for each the rows of ``query``."""

        families = families_for(query, self.resolver)
        agents: list[dict[str, Any]] = []
        for family in families:
            if family == "ledger":
                agents.append(ledger_row())
            else:
                agents.extend(self.query(caller=caller, role=family)["agents"])
        log.info(
            "index.search",
            extra={"agent_id": caller, "msg_id": None, "need_id": None, "families": families},
        )
        return {"families": families, "agents": agents}

    def advance(self, delta: timedelta) -> None:
        if isinstance(self.clock, FixedClock):
            self.clock.moment = self.clock.moment + delta


class TransitionConflict(Exception):
    """Disallowed visibility transition (HTTP 409 per index OpenAPI)."""

    def __init__(self, message: str = "") -> None:
        self.message = message
        super().__init__(message)

    def body(self) -> dict[str, str]:
        payload = {"code": "POLICY_VIOLATION"}
        if self.message:
            payload["message"] = self.message
        return payload
