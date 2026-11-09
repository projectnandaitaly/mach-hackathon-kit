"""HTTP surface for ``specs/openapi/deploy.yaml`` plus the CLI commands it needs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from cap.core.errors import CapError
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from city_deploy.errors import Conflict, QuotaExceeded
from city_deploy.image import decode_files
from city_deploy.service import DeployService


def create_app(deploy: DeployService | None = None) -> FastAPI:
    service = deploy or DeployService()
    app = FastAPI(title="City Deploy", version="0.10.0")
    app.state.deploy = service

    @app.middleware("http")
    async def _checkpoint(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        # The CLI polls deployments every half second; a poll changes nothing.
        path = request.url.path
        poll = request.method == "GET" and (path.startswith("/deployments/") or path == "/health")
        if not poll and response.status_code < 500:
            await service.checkpoint()
        return response

    @app.exception_handler(CapError)
    async def _cap_error(_request: Request, exc: CapError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status(), content=exc.body())

    @app.exception_handler(QuotaExceeded)
    async def _quota(_request: Request, exc: QuotaExceeded) -> JSONResponse:
        return JSONResponse(status_code=403, content=exc.body())

    @app.exception_handler(Conflict)
    async def _conflict(_request: Request, exc: Conflict) -> JSONResponse:
        return JSONResponse(status_code=409, content=exc.body())

    @app.post("/login/device")
    async def start_device(request: Request) -> dict[str, str]:
        raw = await request.body()
        body = await _object(request) if raw.strip() else {}
        challenge = body.get("code_challenge")
        method = body.get("code_challenge_method", "S256")
        if challenge is not None and (not isinstance(challenge, str) or method != "S256"):
            raise CapError("SCHEMA_INVALID", "code_challenge")
        return service.start_device(challenge)

    @app.post("/login/approve")
    async def approve_device(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, str]:
        service.authorize_approve(authorization)
        body = await _object(request)
        user_code = body.get("user_code")
        team_id = body.get("team_id")
        permissions = body.get("permissions", ["invoke"])
        if not isinstance(user_code, str) or not isinstance(team_id, str):
            raise CapError("SCHEMA_INVALID", "device")
        if not isinstance(permissions, list) or not all(
            isinstance(item, str) for item in permissions
        ):
            raise CapError("SCHEMA_INVALID", "permissions")
        loopback = body.get("loopback", False)
        if not isinstance(loopback, bool):
            raise CapError("SCHEMA_INVALID", "loopback")
        return service.approve_device(
            user_code, team_id, [str(item) for item in permissions], loopback=loopback
        )

    @app.post("/login/poll")
    async def poll_device(request: Request) -> dict[str, str]:
        body = await _object(request)
        device_code = body.get("device_code")
        if not isinstance(device_code, str):
            raise CapError("SCHEMA_INVALID", "device_code")
        return service.poll_device(device_code)

    @app.post("/login/token")
    async def exchange_device(request: Request) -> dict[str, str]:
        body = await _object(request)
        device_code = body.get("device_code")
        auth_code = body.get("auth_code")
        verifier = body.get("code_verifier")
        if (
            not isinstance(device_code, str)
            or not isinstance(auth_code, str)
            or not isinstance(verifier, str)
        ):
            raise CapError("SCHEMA_INVALID", "token exchange")
        return service.exchange_device(device_code, auth_code, verifier)

    @app.post("/keys/register")
    async def register_controller(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, str]:
        body = await _object(request)
        jwk = body.get("jwk")
        retire = body.get("retire")
        if not isinstance(jwk, dict):
            raise CapError("SCHEMA_INVALID", "jwk")
        if retire is not None and not isinstance(retire, dict):
            raise CapError("SCHEMA_INVALID", "retire")
        return service.register_controller(_bearer(authorization), jwk, retire=retire)

    @app.get("/teams/me/llm-key")
    async def team_llm_key(
        authorization: str | None = Header(default=None),
    ) -> dict[str, str]:
        return service.team_llm_key(_bearer(authorization))

    @app.post("/agent-ids")
    async def assign_agent_id(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, str]:
        body = await _object(request)
        family = body.get("family")
        if not isinstance(family, str) or set(body) != {"family"}:
            raise CapError("SCHEMA_INVALID", "family")
        return service.assign_agent_id(_bearer(authorization), family)

    @app.get("/agent-ids")
    async def list_agent_ids(
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        return service.list_agent_ids(_bearer(authorization))

    @app.post("/agents/{agent_id:path}/keys")
    async def mint_agent_key(
        agent_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, str]:
        return service.mint_agent_key(_bearer(authorization), agent_id)

    @app.post("/agents/{agent_id:path}/keys/next")
    async def prepare_rotation(
        agent_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, str]:
        return service.prepare_rotation(_bearer(authorization), agent_id)

    @app.post("/agents/{agent_id:path}/keys/rotate")
    async def confirm_rotation(
        agent_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, str]:
        body = await _object(request)
        return await service.confirm_rotation(_bearer(authorization), agent_id, body)

    @app.post("/deployments", status_code=202)
    async def create_deployment(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        body = await _object(request)
        return service.create_deployment(_bearer(authorization), body)

    @app.get("/deployments/{deployment_id}")
    async def get_deployment(
        deployment_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        return service.get_deployment(_bearer(authorization), deployment_id)

    @app.post("/uploads/{deployment_id}")
    async def complete_upload(
        deployment_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        body = await _object(request)
        dockerfile = body.get("dockerfile")
        size = body.get("size_bytes", 0)
        pinned = body.get("image_digest")
        wait = body.get("wait", True)
        if not isinstance(dockerfile, str) or not isinstance(size, int):
            raise CapError("SCHEMA_INVALID", "upload")
        if pinned is not None and not isinstance(pinned, str):
            raise CapError("SCHEMA_INVALID", "image_digest")
        if not isinstance(wait, bool):
            raise CapError("SCHEMA_INVALID", "wait")
        raw_files = body.get("files")
        files = None if raw_files is None else decode_files(raw_files)
        return await service.complete_upload(
            _bearer(authorization),
            deployment_id,
            dockerfile=dockerfile,
            size_bytes=size,
            image_digest=pinned,
            files=files,
            wait=wait,
        )

    @app.post("/agents/{agent_id:path}/synthetic-need")
    async def synthetic_need(
        agent_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        body = await _object(request)
        recipient = body.get("to")
        grant_id = body.get("grant_id")
        need_id = body.get("need_id")
        payload = body.get("body", {"service": "lab", "summary": "visit"})
        if (
            not isinstance(recipient, str)
            or not isinstance(grant_id, str)
            or not isinstance(need_id, str)
        ):
            raise CapError("SCHEMA_INVALID", "need")
        if not isinstance(payload, dict):
            raise CapError("SCHEMA_INVALID", "need")
        return service.synthetic_need(
            _bearer(authorization),
            agent_id,
            recipient=recipient,
            grant_id=grant_id,
            need_id=need_id,
            body={str(key): value for key, value in payload.items()},
        )

    @app.post("/agents/{agent_id:path}/rollback", status_code=202)
    async def rollback_agent(
        agent_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        body = await _object(request)
        version = body.get("version")
        if version is not None and not isinstance(version, str):
            raise CapError("SCHEMA_INVALID", "version")
        return await service.rollback(_bearer(authorization), agent_id, version)

    @app.delete("/agents/{agent_id:path}")
    async def delete_agent(
        agent_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        return await service.delete_agent(_bearer(authorization), agent_id)

    @app.get("/agents/{agent_id:path}/status")
    async def agent_status(
        agent_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        return service.status(_bearer(authorization), agent_id)

    @app.post("/agents/{agent_id:path}/scale")
    async def scale_agent(
        agent_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        body = await _object(request)
        return service.scale(
            _bearer(authorization),
            agent_id,
            min_replicas=_int_field(body, "min"),
            max_replicas=_int_field(body, "max"),
            concurrency=_int_field(body, "concurrency"),
        )

    @app.post("/agents/{agent_id:path}/secrets")
    async def set_secret(
        agent_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, str]:
        body = await _object(request)
        name = body.get("key")
        value = body.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            raise CapError("SCHEMA_INVALID", "secret")
        return service.set_secret(_bearer(authorization), agent_id, name, value)

    @app.post("/agents/{agent_id:path}/llm-key/rotate")
    async def rotate_llm(
        agent_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, str]:
        return service.rotate_llm(_bearer(authorization), agent_id)

    @app.post("/agents/{agent_id:path}/llm-key/revoke")
    async def revoke_llm(
        agent_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, str]:
        return service.revoke_llm(_bearer(authorization), agent_id)

    @app.get("/agents/{agent_id:path}/compliance")
    async def compliance(
        agent_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        return service.compliance(_bearer(authorization), agent_id)

    return app


def _bearer(authorization: str | None) -> str:
    if authorization is None or not authorization.startswith("Bearer "):
        raise CapError("POLICY_VIOLATION", "team token required")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise CapError("POLICY_VIOLATION", "team token required")
    return token


async def _object(request: Request) -> dict[str, Any]:
    body = await request.json()
    if not isinstance(body, dict):
        raise CapError("SCHEMA_INVALID", "body")
    return body


def _int_field(body: Mapping[str, Any], name: str) -> int:
    value = body.get(name)
    if type(value) is not int:
        raise CapError("SCHEMA_INVALID", name)
    return value
