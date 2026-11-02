"""Typed listen ports for the live two-agent path.

Cloud Run and GKE may override the port with ``PORT``. The catalog is the
local binding used when two template agents and the city HTTP services share
one host.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from cap.core.errors import CapError


class Plane(StrEnum):
    RELAY = "relay"
    INDEX = "index"
    ATTESTATION = "attestation"
    DEPLOY = "deploy"
    CONSOLE = "console"
    BOOKING_CENTRE = "booking_centre"
    FACILITY = "facility"
    PROBER = "prober"
    DEMAND = "demand"
    SEALER = "sealer"
    GOVERNANCE = "governance"
    LEDGER = "ledger"
    LLM = "llm"
    DISPATCHER = "dispatcher"
    LEASE_SCANNER = "lease-scanner"
    REPUTATION = "reputation"


@dataclass(frozen=True)
class HttpListen:
    plane: Plane
    port: int

    def url(self, host: str = "127.0.0.1") -> str:
        return f"http://{host}:{self.port}"


LISTEN: dict[Plane, HttpListen] = {
    Plane.RELAY: HttpListen(Plane.RELAY, 8081),
    Plane.INDEX: HttpListen(Plane.INDEX, 8082),
    Plane.ATTESTATION: HttpListen(Plane.ATTESTATION, 8083),
    Plane.DEPLOY: HttpListen(Plane.DEPLOY, 8084),
    Plane.CONSOLE: HttpListen(Plane.CONSOLE, 8085),
    Plane.BOOKING_CENTRE: HttpListen(Plane.BOOKING_CENTRE, 8091),
    Plane.FACILITY: HttpListen(Plane.FACILITY, 8092),
    Plane.PROBER: HttpListen(Plane.PROBER, 8093),
    Plane.DEMAND: HttpListen(Plane.DEMAND, 8094),
    Plane.SEALER: HttpListen(Plane.SEALER, 8095),
    Plane.GOVERNANCE: HttpListen(Plane.GOVERNANCE, 8096),
    Plane.LEDGER: HttpListen(Plane.LEDGER, 8097),
    Plane.LLM: HttpListen(Plane.LLM, 8098),
    Plane.DISPATCHER: HttpListen(Plane.DISPATCHER, 8099),
    Plane.LEASE_SCANNER: HttpListen(Plane.LEASE_SCANNER, 8100),
    Plane.REPUTATION: HttpListen(Plane.REPUTATION, 8101),
}

TWO_AGENT: tuple[Plane, ...] = (
    Plane.RELAY,
    Plane.INDEX,
    Plane.ATTESTATION,
    Plane.BOOKING_CENTRE,
    Plane.FACILITY,
)

AGENT_CONTAINER_PORT = 8080


def endpoint(plane: Plane, *, host: str = "127.0.0.1") -> str:
    return LISTEN[plane].url(host)


def two_agent_urls(host: str = "127.0.0.1") -> dict[str, str]:
    return {plane.value: endpoint(plane, host=host) for plane in TWO_AGENT}


def listen(plane: Plane, environ: Mapping[str, str] | None = None) -> tuple[str, int]:
    env = os.environ if environ is None else environ
    host = env.get("HOST", "0.0.0.0")
    raw = env.get("PORT", "")
    if raw == "":
        return host, LISTEN[plane].port
    if not raw.isdigit():
        raise CapError("SCHEMA_INVALID", "PORT")
    port = int(raw)
    if port < 1 or port > 65535:
        raise CapError("SCHEMA_INVALID", "PORT")
    return host, port
