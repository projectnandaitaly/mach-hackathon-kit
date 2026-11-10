"""Production Deploy HTTP process. Memory stays the default runtime."""

from __future__ import annotations

import os

from cap.core.errors import CapError
from cap.core.http_ports import Plane, listen
from fastapi import FastAPI

from city_deploy.api import create_app
from city_deploy.conform import CandidateSuite
from city_deploy.runtime import DRIVERS, GkeRuntime, KubernetesClient, MemoryRuntime
from city_deploy.service import DeployService
from city_deploy.stall import StallWatch
from city_deploy.teams import (
    OpenTeamDirectory,
    TeamDirectory,
    market_lock_for,
    open_firestore_team_directory,
)


def build_service() -> DeployService:
    env = os.environ.get("CITY_ENV", "")
    runtime_name = os.environ.get("CITY_RUNTIME", "gke" if env == "cloud" else "memory")
    teams: TeamDirectory = OpenTeamDirectory()
    approve_token: str | None = None
    verification_uri = os.environ.get("CITY_DEVICE_URI", "").strip() or None
    if env == "cloud":
        project = os.environ.get("CITY_GCP_PROJECT", "").strip()
        if not project:
            raise CapError("POLICY_VIOLATION", "CITY_GCP_PROJECT")
        database = os.environ.get("CITY_FIRESTORE_DATABASE", "(default)").strip() or "(default)"
        teams = open_firestore_team_directory(project=project, database=database)
        approve_token = os.environ.get("CITY_DEPLOY_APPROVE_TOKEN", "").strip() or None
        if not approve_token:
            raise CapError("POLICY_VIOLATION", "CITY_DEPLOY_APPROVE_TOKEN")
    if runtime_name == "gke":
        runtime = GkeRuntime(KubernetesClient.from_env())
        return _with_wave_lock(
            DeployService(
                runtime=runtime,
                suite=CandidateSuite(runtime),
                teams=teams,
                approve_token=approve_token,
                verification_uri=verification_uri,
            ),
            teams,
        )
    if env == "cloud":
        if runtime_name != "cloudrun":
            raise CapError("POLICY_VIOLATION", "CITY_RUNTIME")
    if runtime_name not in DRIVERS:
        raise CapError("SCHEMA_INVALID", "runtime")
    return _with_wave_lock(
        DeployService(
            runtime=MemoryRuntime(runtime_name),
            teams=teams,
            approve_token=approve_token,
            verification_uri=verification_uri,
        ),
        teams,
    )


def _with_wave_lock(service: DeployService, teams: TeamDirectory) -> DeployService:
    locked = market_lock_for(teams)
    if locked is not None:
        service.market_locked = locked
    return service


def build_app() -> FastAPI:
    app = create_app(build_service())
    watch = StallWatch()

    @app.on_event("startup")
    async def _watch_loop() -> None:
        watch.start()

    @app.on_event("shutdown")
    async def _unwatch_loop() -> None:
        watch.stop()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "deploy"}

    return app


def main() -> None:
    import uvicorn

    host, port = listen(Plane.DEPLOY)
    uvicorn.run(build_app(), host=host, port=port)
