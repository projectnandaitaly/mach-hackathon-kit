"""Team purge for City Hall (ADR-050; ``deploy.yaml`` ``POST /teams/{team_id}/purge``)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from cap.core.errors import CapError
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry import trace

from city_deploy.service import DeployService

log = logging.getLogger("deploy")
tracer = trace.get_tracer("city.deploy")

CONSOLE = "city:console"
CityCallers = Callable[[Request], str | None]


def add_purge_routes(app: FastAPI, service: DeployService, *, callers: CityCallers) -> None:
    @app.post("/teams/{team_id}/purge")
    async def purge_team(team_id: str, request: Request) -> JSONResponse:
        caller = callers(request)
        try:
            if caller != CONSOLE:
                raise CapError("GRANT_PERMISSION_DENIED", "city-only route")
            dry_run = _dry_run(await request.body())
            with tracer.start_as_current_span(
                "deploy.team.purge", attributes={"agent_id": caller, "team_id": team_id}
            ):
                summary = await service.purge_team(team_id, dry_run=dry_run)
        except CapError as error:
            return JSONResponse(status_code=error.http_status(), content=error.body())
        log.info(
            "deploy.team.purge",
            extra={
                "need_id": None,
                "msg_id": None,
                "agent_id": caller,
                "team_id": team_id,
                "dry_run": dry_run,
                "agents": len(summary["agents"]),
            },
        )
        return JSONResponse(status_code=200, content=summary)

    @app.post("/agents/{agent_id:path}/delete")
    async def delete_agent(agent_id: str, request: Request) -> JSONResponse:
        caller = callers(request)
        try:
            if caller != CONSOLE:
                raise CapError("GRANT_PERMISSION_DENIED", "city-only route")
            with tracer.start_as_current_span(
                "deploy.agent.delete", attributes={"agent_id": agent_id}
            ):
                summary = await service.delete_agent(None, agent_id, console=True)
        except CapError as error:
            return JSONResponse(status_code=error.http_status(), content=error.body())
        log.info(
            "deploy.agent.delete",
            extra={"need_id": None, "msg_id": None, "agent_id": agent_id},
        )
        return JSONResponse(status_code=200, content=summary)

    @app.get("/teams/{team_id}/deployments")
    async def list_deployments(team_id: str, request: Request) -> JSONResponse:
        caller = callers(request)
        try:
            if caller != CONSOLE:
                raise CapError("GRANT_PERMISSION_DENIED", "city-only route")
            with tracer.start_as_current_span(
                "deploy.team.deployments", attributes={"agent_id": caller, "team_id": team_id}
            ):
                listed = service.team_deployments(team_id)
        except CapError as error:
            return JSONResponse(status_code=error.http_status(), content=error.body())
        return JSONResponse(status_code=200, content=listed)


def _dry_run(raw: bytes) -> bool:
    try:
        body = json.loads(raw) if raw.strip() else {}
    except ValueError as exc:
        raise CapError("SCHEMA_INVALID", "body") from exc
    if not isinstance(body, dict) or set(body) - {"dry_run"}:
        raise CapError("SCHEMA_INVALID", "body")
    dry_run = body.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise CapError("SCHEMA_INVALID", "dry_run")
    return dry_run
