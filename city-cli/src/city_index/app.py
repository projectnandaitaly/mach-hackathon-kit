"""FastAPI surface for the City Index (``specs/openapi/index.yaml``)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from cap.core.errors import CapError
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

from city_index.catalog import assert_complete_card
from city_index.service import IndexService, TransitionConflict

log = logging.getLogger("city.index")
_QUERY_PARAMS = frozenset({"role", "include"})


def create_app(index: IndexService, *, allow_header_mutations: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async def sync() -> None:
            while True:
                try:
                    await index.sync_watch_once()
                    await index.drain_outbox_once()
                except Exception:
                    log.exception("index snapshot refresh failed")
                await asyncio.sleep(1)

        app.state.snapshot_sync = asyncio.create_task(sync())
        try:
            yield
        finally:
            app.state.snapshot_sync.cancel()

    app = FastAPI(title="City Index", version="0.10.0", lifespan=lifespan)
    app.state.index = index

    @app.exception_handler(CapError)
    async def _cap_error(_request: Request, exc: CapError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status(), content=exc.body())

    @app.exception_handler(TransitionConflict)
    async def _transition(_request: Request, exc: TransitionConflict) -> JSONResponse:
        return JSONResponse(status_code=409, content=exc.body())

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        try:
            return JSONResponse(index.ready())
        except Exception:
            return JSONResponse(status_code=503, content={"status": "not_ready"})

    @app.get("/v1/agents")
    async def query_agents(request: Request) -> dict[str, Any]:
        params = request.query_params
        unknown = sorted(set(params) - _QUERY_PARAMS)
        if unknown:
            raise CapError("SCHEMA_INVALID", f"unknown query parameter {unknown[0]}")
        if any(len(params.getlist(name)) > 1 for name in params):
            raise CapError("SCHEMA_INVALID", "repeated query parameter")
        role = params.get("role")
        if role is None:
            raise CapError("SCHEMA_INVALID", "role required")
        caller = request.headers.get("X-Caller", "anonymous")
        return index.query(caller=caller, role=role, include=params.get("include"))

    @app.get("/v1/agents/{agent_id:path}/card")
    async def get_card(agent_id: str) -> dict[str, Any]:
        return index.pinned_card(agent_id)

    @app.patch("/v1/agents/{agent_id:path}/visibility")
    async def patch_visibility(
        agent_id: str,
        request: Request,
        x_city_role: str | None = Header(default=None, alias="X-City-Role"),
    ) -> dict[str, Any]:
        if not allow_header_mutations:
            raise CapError("POLICY_VIOLATION", "public mutation endpoint disabled")
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("visibility"), str):
            raise CapError("SCHEMA_INVALID", "visibility required")
        return await index.patch_visibility(
            agent_id,
            str(body["visibility"]),
            role_header=x_city_role,
            cause=str(body.get("cause", "order")),
        )

    @app.put("/v1/agents/{agent_id:path}")
    async def put_agent(
        agent_id: str,
        request: Request,
        x_city_role: str | None = Header(default=None, alias="X-City-Role"),
    ) -> dict[str, Any]:
        if not allow_header_mutations:
            raise CapError("POLICY_VIOLATION", "public mutation endpoint disabled")
        body = await request.json()
        if not isinstance(body, dict):
            raise CapError("SCHEMA_INVALID", "body must be an object")
        if body.get("agent_id") != agent_id:
            raise CapError("SCHEMA_INVALID", "path agent_id must match card")
        assert_complete_card(body)
        return await index.put_agent(body, role_header=x_city_role)

    @app.get("/v1/agents/{agent_id:path}")
    async def get_agent(agent_id: str) -> dict[str, Any]:
        return index.agent_record(agent_id)

    @app.get("/v1/keys/{kid:path}")
    async def get_key(kid: str) -> JSONResponse:
        try:
            return JSONResponse(index.get_key(kid))
        except CapError as exc:
            if exc.code == "KEY_UNKNOWN":
                return JSONResponse(status_code=404, content=exc.body())
            raise

    @app.get("/v1/watch")
    async def watch(
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        queue = index.watch.subscribe(last_event_id=last_event_id)

        async def gen() -> Any:
            async for chunk in index.watch.stream(queue):
                yield chunk

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app
