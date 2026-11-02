"""Per-wave citizen grants, city-only (ADR-039; ``deploy.yaml`` ``POST /citizen-grants``)."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any, Protocol

from cap.core.errors import CapError
from cap.core.keys import PublicKey
from cap.core.timeutil import format_time, parse_time
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

log = logging.getLogger("deploy")

DIRECTOR = "city:director"
PERMISSIONS = ["invoke", "rate"]
MAX_CITIZENS = 1000
MAX_LIFETIME = timedelta(hours=24)
_WAVE = re.compile(r"^W[0-9A-Z]{1,8}$")
_CITIZEN = re.compile(r"^citizen:[A-Z0-9-]+$")
_REQUEST = frozenset({"wave_id", "not_after", "citizens"})
_ROW = frozenset({"citizen_id", "citizen_pubkey"})
_JWK = frozenset({"kty", "crv", "x"})


class CitizenGrantSink(Protocol):
    def put_citizen_grants(self, grants: list[dict[str, Any]]) -> None: ...


CityCallers = Callable[[Request], str | None]


def issue_citizen_grants(body: object, *, now: datetime) -> list[dict[str, Any]]:
    """One grant per citizen, in request order. Only public halves cross the wire."""

    if not isinstance(body, Mapping) or set(body) != _REQUEST:
        raise CapError("SCHEMA_INVALID", "citizen grant request")
    wave = body["wave_id"]
    if not isinstance(wave, str) or _WAVE.fullmatch(wave) is None:
        raise CapError("SCHEMA_INVALID", "wave_id")
    not_after = parse_time(body["not_after"]) if isinstance(body["not_after"], str) else None
    if not_after is None or not now < not_after <= now + MAX_LIFETIME:
        raise CapError("SCHEMA_INVALID", "not_after")
    rows = body["citizens"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_CITIZENS:
        raise CapError("SCHEMA_INVALID", "citizens")
    grants: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        citizen_id, jwk = _row(row, wave)
        if citizen_id in seen:
            raise CapError("SCHEMA_INVALID", "citizen listed twice")
        seen.add(citizen_id)
        grants.append(
            {
                "grant_id": f"cg:{wave}/{citizen_id.removeprefix('citizen:')}",
                "citizen_id": citizen_id,
                "wave_id": wave,
                "citizen_pubkey": jwk,
                "permissions": list(PERMISSIONS),
                "not_before": format_time(now),
                "not_after": format_time(not_after),
            }
        )
    return grants


def _row(row: object, wave: str) -> tuple[str, dict[str, str]]:
    if not isinstance(row, Mapping) or set(row) != _ROW:
        raise CapError("SCHEMA_INVALID", "citizen")
    citizen_id = row["citizen_id"]
    if (
        not isinstance(citizen_id, str)
        or _CITIZEN.fullmatch(citizen_id) is None
        or not citizen_id.startswith(f"citizen:{wave}-")
    ):
        raise CapError("SCHEMA_INVALID", "citizen_id")
    jwk = row["citizen_pubkey"]
    if (
        not isinstance(jwk, Mapping)
        or set(jwk) != _JWK
        or jwk.get("kty") != "OKP"
        or jwk.get("crv") != "Ed25519"
        or not isinstance(jwk.get("x"), str)
    ):
        raise CapError("SCHEMA_INVALID", "citizen_pubkey")
    public = {"kty": "OKP", "crv": "Ed25519", "x": str(jwk["x"])}
    try:
        PublicKey.from_jwk(public)
    except (CapError, ValueError) as exc:
        raise CapError("SCHEMA_INVALID", "citizen_pubkey") from exc
    return citizen_id, public


def add_citizen_routes(
    app: FastAPI,
    grants: CitizenGrantSink,
    *,
    callers: CityCallers,
    now: Callable[[], datetime],
) -> None:
    @app.post("/citizen-grants", status_code=201)
    async def issue(request: Request) -> JSONResponse:
        try:
            if callers(request) != DIRECTOR:
                raise CapError("GRANT_PERMISSION_DENIED", "city-only route")
            try:
                body = await request.json()
            except ValueError as exc:
                raise CapError("SCHEMA_INVALID", "body must be JSON") from exc
            issued = issue_citizen_grants(body, now=now())
        except CapError as exc:
            return JSONResponse(status_code=exc.http_status(), content=exc.body())
        grants.put_citizen_grants(issued)
        log.info(
            "citizen.grants",
            extra={
                "need_id": None,
                "msg_id": issued[0]["grant_id"],
                "agent_id": DIRECTOR,
                "count": len(issued),
            },
        )
        return JSONResponse(status_code=201, content={"grants": issued})
