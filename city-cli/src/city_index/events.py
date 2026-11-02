"""City-event builders for Index mutations."""

from __future__ import annotations

import hashlib
from typing import Any


def _record_id(event_type: str, subject: str, at: str, detail: str) -> str:
    raw = "\0".join((event_type, subject, at, detail)).encode("utf-8")
    return "event:" + hashlib.sha256(raw).hexdigest()


def visibility_changed(
    *,
    agent_id: str,
    from_state: str,
    to_state: str,
    cause: str,
    observer: str,
    at: str,
) -> dict[str, Any]:
    # ADR-067: agent_id / from / to also sit at the top level so the public
    # attestation copy (which drops payload) can feed the LIVE ticker.
    return {
        "record_kind": "event",
        "record_id": _record_id("visibility.changed", agent_id, at, f"{from_state}>{to_state}"),
        "type": "visibility.changed",
        "observer": observer,
        "time": at,
        "agent_id": agent_id,
        "from": from_state,
        "to": to_state,
        "payload": {
            "agent_id": agent_id,
            "from": from_state,
            "to": to_state,
            "cause": cause,
        },
    }


def agent_deleted(*, agent_id: str, cause: str, observer: str, at: str) -> dict[str, Any]:
    return {
        "record_kind": "event",
        "record_id": _record_id("agent.deleted", agent_id, at, cause),
        "type": "agent.deleted",
        "observer": observer,
        "time": at,
        "agent_id": agent_id,
        "payload": {"agent_id": agent_id, "cause": cause},
    }


def agent_promoted(
    *,
    agent_id: str,
    version: str,
    observer: str,
    at: str,
    team: str = "",
    family: str | None = None,
) -> dict[str, Any]:
    # ADR-085: the id carries no team, so the log names it. ``team`` also sits at
    # the top level, which the public attestation copy keeps.
    record: dict[str, Any] = {
        "record_kind": "event",
        "record_id": _record_id("agent.promoted", agent_id, at, version),
        "type": "agent.promoted",
        "observer": observer,
        "time": at,
        "agent_id": agent_id,
        "payload": {"agent_id": agent_id, "version": version},
    }
    if team:
        record["team"] = team
        record["payload"]["team"] = team
    if family is not None:
        record["family"] = family
        record["payload"]["family"] = family
    return record
