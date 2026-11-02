"""Local-only ``POST /dev/invoke`` and in-process chat invoke. Never on cloud."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from cap.core.envelope import build, sign
from cap.core.errors import CapError
from cap.core.keys import PrivateKey, generate
from cap.core.timeutil import format_time
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from city_sdk.harness import Harness
from city_sdk.ports import MemoryCare, MemoryLedger, MemoryRelay

LOCAL_SENDER = "agent:local/dev"
CITIZEN_SENDER = "citizen:local/dev"
_NEED_FIELDS = frozenset({"service", "district", "summary", "is_emergency"})
_RICH_FIELDS = frozenset({"type", "body", "from", *_NEED_FIELDS})
_MESSAGE_TYPES = frozenset(
    {
        "need.request",
        "checkin",
        "confirm",
        "data.response",
        "consent",
        "offer",
        "cancel",
    }
)


@dataclass(frozen=True)
class LocalMessage:
    message_type: str
    sender: str
    body: dict[str, object]


def mount_dev_invoke(app: FastAPI, harness: Harness) -> None:
    """Accept a local payload and deliver it through the in-memory harness."""
    if not isinstance(harness.relay, MemoryRelay) or not isinstance(harness.ledger, MemoryLedger):
        raise CapError("POLICY_VIOLATION", "dev")

    @app.post("/dev/invoke")
    async def dev_invoke(request: Request) -> JSONResponse:
        result = await invoke_local(harness, await _json_object(request))
        status = int(result.pop("http_status"))
        return JSONResponse(status_code=status, content=result)


def need_body(payload: object) -> dict[str, object]:
    """Plain Need fields only (legacy ``/dev/invoke`` shape)."""
    if not isinstance(payload, dict):
        raise CapError("SCHEMA_INVALID", "body must be an object")
    for key in payload:
        if key not in _NEED_FIELDS:
            raise CapError("SCHEMA_INVALID", "unknown field")
    service = payload.get("service", "")
    summary = payload.get("summary", "")
    if not isinstance(service, str):
        raise CapError("SCHEMA_INVALID", "service")
    if not isinstance(summary, str):
        raise CapError("SCHEMA_INVALID", "summary")
    body: dict[str, object] = {"service": service, "summary": summary}
    if "district" in payload:
        district = payload["district"]
        if type(district) is not int:
            raise CapError("SCHEMA_INVALID", "district")
        body["district"] = district
    if "is_emergency" in payload:
        flag = payload["is_emergency"]
        if type(flag) is not bool:
            raise CapError("SCHEMA_INVALID", "is_emergency")
        body["is_emergency"] = flag
    return body


def parse_local_payload(payload: object) -> LocalMessage:
    """Accept a plain Need or a richer care-journey local message."""
    if not isinstance(payload, dict):
        raise CapError("SCHEMA_INVALID", "body must be an object")
    if "type" in payload or "body" in payload:
        return _parse_rich(payload)
    return LocalMessage(
        message_type="need.request",
        sender=LOCAL_SENDER,
        body=need_body(payload),
    )


def _parse_rich(payload: dict[str, object]) -> LocalMessage:
    for key in payload:
        if key not in _RICH_FIELDS:
            raise CapError("SCHEMA_INVALID", "unknown field")
    raw_type = payload.get("type", "need.request")
    if not isinstance(raw_type, str) or raw_type not in _MESSAGE_TYPES:
        raise CapError("SCHEMA_INVALID", "type")
    raw_body = payload.get("body", {})
    if not isinstance(raw_body, dict):
        raise CapError("SCHEMA_INVALID", "body")
    body: dict[str, object] = dict(raw_body)
    for key in _NEED_FIELDS:
        if key not in payload:
            continue
        value = payload[key]
        if key in {"service", "summary"}:
            if not isinstance(value, str):
                raise CapError("SCHEMA_INVALID", key)
            body[key] = value
        elif key == "district":
            if type(value) is not int:
                raise CapError("SCHEMA_INVALID", "district")
            body[key] = value
        elif key == "is_emergency":
            if type(value) is not bool:
                raise CapError("SCHEMA_INVALID", "is_emergency")
            body[key] = value
    sender = payload.get("from", CITIZEN_SENDER)
    if not isinstance(sender, str) or not sender:
        raise CapError("SCHEMA_INVALID", "from")
    return LocalMessage(message_type=raw_type, sender=sender, body=body)


async def invoke_local(harness: Harness, payload: object) -> dict[str, Any]:
    """Deliver one local message through the in-memory harness and return actions."""
    if not isinstance(harness.relay, MemoryRelay) or not isinstance(harness.ledger, MemoryLedger):
        raise CapError("POLICY_VIOLATION", "dev")
    message = parse_local_payload(payload)
    pair = generate()
    kid = message.sender + "#k1"
    harness.keys[kid] = pair.public
    relay = harness.relay
    ledger = harness.ledger
    care = harness.care if isinstance(harness.care, MemoryCare) else None
    sent = len(relay.sent)
    posted = len(relay.posted)
    calls = len(ledger.calls)
    care_calls = len(care.calls) if care is not None else 0
    escalations = len(harness.escalations)
    status, outcome = await harness.invoke(
        _delivery(
            harness,
            message,
            private=pair.private,
            kid=kid,
        )
    )
    return {
        "http_status": status,
        "status": outcome.get("status"),
        "actions": local_actions(
            harness,
            sent=sent,
            posted=posted,
            calls=calls,
            care_calls=care_calls,
            escalations=escalations,
        ),
    }


def local_actions(
    harness: Harness,
    *,
    sent: int,
    calls: int,
    escalations: int,
    posted: int = 0,
    care_calls: int = 0,
) -> list[dict[str, str]]:
    relay = harness.relay
    ledger = harness.ledger
    if not isinstance(relay, MemoryRelay) or not isinstance(ledger, MemoryLedger):
        raise CapError("POLICY_VIOLATION", "dev")
    actions: list[dict[str, str]] = []
    for agent_id, _body, _trace in relay.sent[sent:]:
        actions.append({"kind": "send", "peer": agent_id})
    for row in relay.posted[posted:]:
        actions.append(
            {
                "kind": "post",
                "to": str(row.get("to", "")),
                "type": str(row.get("type", "")),
            }
        )
    for kind, target in ledger.calls[calls:]:
        if kind == "hold":
            actions.append({"kind": kind, "peer": target})
        else:
            actions.append({"kind": kind, "hold": target})
    care = harness.care
    if isinstance(care, MemoryCare):
        for kind, detail in care.calls[care_calls:]:
            actions.append({"kind": str(kind), "detail": _care_detail(detail)})
    for reason in harness.escalations[escalations:]:
        actions.append({"kind": "escalate", "reason": reason})
    return actions


def _care_detail(detail: object) -> str:
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict):
        for key in (
            "prescription_number",
            "booking_id",
            "hold_id",
            "facility_id",
            "procedure",
        ):
            value = detail.get(key)
            if isinstance(value, str) and value:
                return value
        return json.dumps(detail, sort_keys=True)[:120]
    return str(detail)


async def _json_object(request: Request) -> object:
    raw = await request.body()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CapError("SCHEMA_INVALID", "body must be an object") from exc


def _delivery(
    harness: Harness,
    message: LocalMessage,
    *,
    private: PrivateKey,
    kid: str,
) -> dict[str, Any]:
    msg_id = str(uuid4())
    now = harness.clock.now()
    profiles = harness.card.get("profiles")
    profile = profiles[0] if isinstance(profiles, list) and profiles else None
    profile_name = profile if isinstance(profile, str) else None
    envelope = sign(
        build(
            msg_id=msg_id,
            message_type=message.message_type,
            sender=message.sender,
            recipient=harness.agent_id,
            sent_at=now,
            body=message.body,
            profile=profile_name,
            need_id="local",
        ),
        private,
        kid=kid,
    )
    return {
        "delivery": {
            "msg_id": msg_id,
            "mailbox": "local",
            "fence": 1,
            "attempt": 1,
            "lease_until": format_time(now),
        },
        "envelope": envelope,
    }
