"""FastAPI surface for the agent harness (``specs/openapi/agent.yaml``)."""

from __future__ import annotations

from typing import Any

from cap.core.errors import CapError
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from city_sdk.devmode import mount_dev_invoke
from city_sdk.harness import Harness


def create_app(harness: Harness, *, dev: bool = False) -> FastAPI:
    app = FastAPI(title="CAP agent", version="0.9.0")
    app.state.harness = harness
    if dev:
        mount_dev_invoke(app, harness)

    @app.exception_handler(CapError)
    async def _cap_error(_request: Request, exc: CapError) -> JSONResponse:
        response = JSONResponse(status_code=exc.http_status(), content=exc.body())
        if exc.http_status() in (429, 503):
            response.headers["Retry-After"] = "1"
        return response

    @app.get("/health")
    async def health(probe: str | None = Query(default=None)) -> dict[str, Any]:
        return harness.health(probe=probe)

    @app.post("/invoke")
    async def invoke(request: Request) -> JSONResponse:
        status, body = await harness.invoke(await request.json())
        return JSONResponse(status_code=status, content=body)

    @app.post("/alter")
    async def alter(request: Request) -> JSONResponse:
        status, body = await harness.alter(await request.json())
        return JSONResponse(status_code=status, content=body)

    @app.get("/card")
    async def card() -> dict[str, Any]:
        return harness.card

    @app.get("/.well-known/agent-card.json")
    async def agent_card_alias() -> dict[str, Any]:
        return harness.card

    @app.get("/audit")
    async def audit(need_id: str | None = None) -> dict[str, Any]:
        return {"entries": harness.audit_entries(need_id)}

    return app
