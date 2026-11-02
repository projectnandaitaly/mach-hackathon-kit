"""Confirmed rosters for the director (ADR-044; ``deploy.yaml`` ``GET /rosters``)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from cap.core.errors import CapError
from city_index.families import FAMILIES
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry import trace

from city_deploy.citizen_grant import DIRECTOR
from city_deploy.teams import TeamDirectory

log = logging.getLogger("deploy")
tracer = trace.get_tracer("city.deploy")

CityCallers = Callable[[Request], str | None]


def roster_after_release(
    assignment: Mapping[str, Any] | None,
    *,
    team_id: str,
    family: str,
    agent_id: str,
    service_type: str | None,
) -> list[dict[str, str]] | None:
    """Agents after this release, or None when the roster must stay untouched.

    None means there is no ASST or the family is not one of the five. The caller
    passes the deployment's own team; the id carries none (ADR-085). One released
    agent is enough: the other families stay.
    """

    if family not in FAMILIES or not agent_id.startswith("agent:") or not team_id:
        return None
    if assignment is None or not isinstance(assignment.get("asst"), str):
        return None
    entry: dict[str, str] = {"family": family, "agent_id": agent_id}
    if service_type:
        entry["service_type"] = service_type
    kept: list[dict[str, str]] = []
    if assignment.get("confirmed") is True:
        raw = assignment.get("agents")
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, Mapping) or item.get("family") not in FAMILIES:
                continue
            if item.get("family") == family:
                continue
            row = {"family": str(item["family"]), "agent_id": str(item["agent_id"])}
            stored = item.get("service_type")
            if isinstance(stored, str) and stored:
                row["service_type"] = stored
            kept.append(row)
    return sorted([*kept, entry], key=lambda row: FAMILIES.index(row["family"]))


def roster(team_id: str, assignment: Mapping[str, Any]) -> dict[str, Any]:
    """One ``Roster``: the confirmed agents and the families left to the reference ASST."""

    confirmed = assignment.get("confirmed") is True
    agents: list[dict[str, str]] = []
    if confirmed:
        raw = assignment.get("agents")
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, Mapping) and item.get("family") in FAMILIES:
                row = {"family": str(item["family"]), "agent_id": str(item["agent_id"])}
                if isinstance(item.get("service_type"), str) and item["service_type"]:
                    row["service_type"] = str(item["service_type"])
                agents.append(row)
    served = {row["family"] for row in agents}
    body: dict[str, Any] = {
        "team_id": team_id,
        "asst": str(assignment["asst"]),
        "confirmed": confirmed,
        "agents": agents,
        "fallback": [family for family in FAMILIES if family not in served],
    }
    confirmed_at = assignment.get("confirmed_at")
    if confirmed and isinstance(confirmed_at, str):
        body["confirmed_at"] = confirmed_at
    return body


def add_roster_routes(app: FastAPI, teams: TeamDirectory, *, callers: CityCallers) -> None:
    @app.get("/rosters")
    async def rosters(request: Request) -> JSONResponse:
        caller = callers(request)
        if caller != DIRECTOR:
            error = CapError("GRANT_PERMISSION_DENIED", "city-only route")
            return JSONResponse(status_code=error.http_status(), content=error.body())
        with tracer.start_as_current_span("deploy.rosters", attributes={"agent_id": caller}):
            rows = [roster(team_id, assigned) for team_id, assigned in teams.assignments()]
        log.info(
            "deploy.rosters",
            extra={"need_id": None, "msg_id": None, "agent_id": caller, "count": len(rows)},
        )
        return JSONResponse(status_code=200, content={"rosters": rows})
