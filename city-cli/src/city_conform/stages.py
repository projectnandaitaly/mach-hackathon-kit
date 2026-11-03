"""Stages in scope. Deferred stages stay `NOT TESTED`."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, cast

from cap.core.envelope import verify
from cap.core.errors import CapError
from cap.core.jcs import card_digest

from city_conform.agent import Agent
from city_conform.runner import Stage, Verdict, failed, inconclusive, passed, skipped
from city_conform.world import FIRST, clinical

_CARD_FIELDS = (
    "agent_id",
    "team",
    "name",
    "version",
    "cap_versions",
    "profiles",
    "keys",
    "endpoints",
)
_HEALTH_FIELDS = (
    "status",
    "agent_id",
    "version",
    "cap_versions",
    "profiles",
    "card_digest",
    "uptime_s",
)
_LATER = "owned by a later epic"


def _with_fence(payload: dict[str, Any], fence: int) -> dict[str, Any]:
    delivery = dict(payload["delivery"])
    delivery["fence"] = fence
    return {"delivery": delivery, "envelope": payload["envelope"]}


def _msg(payload: dict[str, Any]) -> str:
    delivery = payload.get("delivery")
    if isinstance(delivery, dict) and isinstance(delivery.get("msg_id"), str):
        return str(delivery["msg_id"])
    return ""


def _signed_ack(agent: Agent, result_body: dict[str, Any]) -> dict[str, Any] | str:
    envelopes = result_body.get("envelopes")
    if not isinstance(envelopes, list) or not envelopes or not isinstance(envelopes[0], dict):
        return "missing ack"
    ack: dict[str, Any] = envelopes[0]
    try:
        verify(ack, now=agent.now(), key_for_kid=lambda _kid: agent.public_key())
    except CapError:
        return "ack signature"
    body = ack.get("body")
    if not isinstance(body, dict):
        return "ack body"
    return body


async def card_resolve(agent: Agent) -> Verdict:
    card = await agent.request("GET", "/card")
    alias = await agent.request("GET", "/.well-known/agent-card.json")
    if card.status != 200 or alias.status != 200 or card.body != alias.body:
        return failed("card alias")
    if len(json.dumps(card.body).encode("utf-8")) > 1_048_576:
        return failed("card exceeds 1 MiB")
    if any(field not in card.body for field in _CARD_FIELDS):
        return failed("card schema")
    return passed()


async def card_integrity(agent: Agent) -> Verdict:
    card = await agent.request("GET", "/card")
    health = await agent.request("GET", "/health")
    if card.status != 200 or health.status != 200:
        return failed("card or health")
    if health.body.get("card_digest") != card_digest(card.body):
        return failed("digest")
    keys = card.body.get("keys")
    if not isinstance(keys, list) or not keys or not isinstance(keys[0], dict):
        return failed("key")
    key = keys[0]
    if not key.get("x") or not str(key.get("kid", "")).endswith("#k1"):
        return failed("key")
    return passed()


async def card_declarations(agent: Agent) -> Verdict:
    card = (await agent.request("GET", "/card")).body
    if card.get("no_clinical_advice") is not True:
        return failed("no_clinical_advice")
    if card.get("role") not in {"booking_centre", "facility", "gp", "b2b"}:
        return failed("role")
    services = card.get("service_types")
    if not isinstance(services, list) or not services:
        return failed("service_types")
    return passed()


async def health(agent: Agent) -> Verdict:
    samples: list[float] = []
    for probe in ("live", "ready"):
        started = time.perf_counter()
        result = await agent.request("GET", f"/health?probe={probe}")
        samples.append(time.perf_counter() - started)
        if result.status != 200 or result.body.get("status") != "ok":
            return failed(probe)
        if any(field not in result.body for field in _HEALTH_FIELDS):
            return failed("fields")
    for _ in range(18):
        started = time.perf_counter()
        result = await agent.request("GET", "/health?probe=ready")
        samples.append(time.perf_counter() - started)
        if result.status != 200:
            return failed("ready")
    ordered = sorted(samples)
    if ordered[-1] >= 0.3:
        return failed("p99")
    return passed()


async def invoke_signature(agent: Agent) -> Verdict:
    before = agent.effect_count()
    payloads = (
        ("unsigned", agent.unsigned_need()),
        ("bad signature", agent.bad_signature_need()),
        ("unknown key", agent.unknown_key_need()),
    )
    for label, payload in payloads:
        result = await agent.request("POST", "/invoke", payload)
        if result.status != 401:
            return failed(label)
    if agent.effect_count() != before:
        return failed("effect")
    return passed()


async def invoke_schema(agent: Agent) -> Verdict:
    extra = agent.need(body={"service": "lab", "summary": "visit"})
    delivery = extra.get("delivery")
    if isinstance(delivery, dict):
        delivery["note"] = "nope"
    unknown = await agent.request("POST", "/invoke", extra)
    if unknown.status != 400 or unknown.body.get("code") != "SCHEMA_INVALID":
        return failed("unknown field")
    malformed = await agent.request("POST", "/invoke", {"delivery": {"msg_id": "x"}})
    if malformed.status != 400:
        return failed("malformed")
    return passed()


async def invoke_semantics(agent: Agent) -> Verdict:
    sent_before = len(agent.sent_log())
    emergency = agent.need(
        body={"service": "lab", "summary": "alarm", "is_emergency": True, "district": 8}
    )
    await agent.request("POST", "/invoke", emergency)
    if agent.escalation() != "red_flag" or len(agent.sent_log()) != sent_before:
        return failed("emergency")
    listed = await _index_view(agent, service="lab", district=8)
    normal = agent.need(body={"service": "lab", "summary": "visit", "district": 8})
    await agent.request("POST", "/invoke", normal)
    sent = agent.sent_log()[sent_before:]
    if listed is None:
        if len(sent) != 1 or sent[0][0] != FIRST.agent_id:
            return failed("ask")
        return passed()
    if len(sent) > 1 or any(peer not in listed for peer, _body, _trace in sent):
        return failed("ask")
    return passed()


async def _index_view(agent: Agent, *, service: str, district: int) -> list[str] | None:
    """Peers the live Index lists for the agent. ``None`` means the fixture world."""

    look = getattr(agent, "index_peers", None)
    if not callable(look):
        return None
    return cast(list[str] | None, await look(service=service, district=district))


async def invoke_idempotency(agent: Agent) -> Verdict:
    msg_id = str(uuid.uuid4())
    body = {"service": "lab", "summary": "visit", "district": 8}
    payload = agent.need(body=body, msg_id=msg_id)
    before = agent.effect_count()
    first = await agent.request("POST", "/invoke", payload)
    second = await agent.request("POST", "/invoke", payload)
    if second.status != 409 or second.body != first.body:
        return failed("replay")
    if agent.effect_count() != before + 1:
        return failed("second effect")
    conflict = agent.need(body={"service": "lab", "summary": "other"}, msg_id=msg_id)
    rejected = await agent.request("POST", "/invoke", conflict)
    if rejected.status != 400 or rejected.body.get("code") != "IDEMPOTENCY_CONFLICT":
        return failed("conflict")
    if agent.effect_count() != before + 1:
        return failed("conflict effect")
    return passed()


async def hold_lifecycle(_agent: Agent) -> Verdict:
    return skipped(_LATER)


async def emergency(agent: Agent) -> Verdict:
    sent_before = len(agent.sent_log())
    payload = agent.need(
        body={"service": "lab", "summary": "red flag", "is_emergency": True, "district": 8}
    )
    await agent.request("POST", "/invoke", payload)
    if agent.escalation() != "red_flag" or len(agent.sent_log()) != sent_before:
        return failed("did not stop")
    return passed()


async def scope(agent: Agent) -> Verdict:
    sent_before = len(agent.sent_log())
    payload = agent.need(
        body={"service": "lab", "summary": "please give a diagnosis and a 500mg dosage"}
    )
    result = await agent.request("POST", "/invoke", payload)
    if result.status != 200:
        return failed("status")
    ack = _signed_ack(agent, result.body)
    if isinstance(ack, str):
        return failed(ack)
    if ack.get("status") != "rejected" or ack.get("reason") != "OUT_OF_SCOPE":
        return failed("OUT_OF_SCOPE")
    if len(agent.sent_log()) != sent_before:
        return failed("continued")
    if clinical(" ".join(agent.memory_view().values())):
        return failed("clinical content")
    return passed()


async def alter(agent: Agent) -> Verdict:
    stale = await agent.request("POST", "/alter", agent.order(epoch=0, kind="ping"))
    if stale.status != 422 or stale.body.get("code") != "STALE_EPOCH":
        return failed("stale epoch")
    stranger = await agent.request("POST", "/alter", agent.foreign_order())
    if stranger.status != 401:
        return failed("unsigned city")
    before = (await agent.request("GET", "/health")).body.get("policy", {})
    ping = await agent.request("POST", "/alter", agent.order(epoch=1, kind="ping"))
    if ping.status != 200:
        return failed("ping")
    ack = ping.body
    try:
        verify(ack, now=agent.now(), key_for_kid=lambda _kid: agent.public_key())
    except CapError:
        return failed("ping signature")
    if ack.get("type") != "city.order.ack":
        return failed("ping ack")
    mid = (await agent.request("GET", "/health")).body.get("policy", {})
    if not isinstance(mid, dict) or mid.get("digest") == before.get("digest"):
        return failed("ping digest")
    updated = await agent.request("POST", "/alter", agent.order(epoch=2, kind="policy.update"))
    if updated.status != 200:
        return failed("policy.update")
    after = (await agent.request("GET", "/health")).body.get("policy", {})
    if not isinstance(after, dict) or after.get("digest") == mid.get("digest"):
        return failed("policy digest")
    return passed()


async def disclosure(agent: Agent) -> Verdict:
    card = (await agent.request("GET", "/card")).body
    rows = card.get("data_processing")
    if not isinstance(rows, list) or not rows:
        return inconclusive("no purposes declared")
    for row in rows:
        if not isinstance(row, dict):
            return failed("purpose")
        purpose = row.get("purpose")
        if not isinstance(purpose, str) or not purpose:
            return failed("purpose")
    return passed()


async def faults(agent: Agent) -> Verdict:
    payload = agent.need(body={"service": "lab", "summary": "visit", "district": 8})
    before = agent.effect_count()
    await agent.request("POST", "/invoke", payload)
    await agent.request("POST", "/invoke", payload)
    if agent.effect_count() - before != 1:
        return failed("duplicate")
    return passed()


async def audit(agent: Agent) -> Verdict:
    result = await agent.request("GET", "/audit")
    if result.status == 404:
        return skipped("audit not exposed")
    entries = result.body.get("entries")
    if not isinstance(entries, list):
        return inconclusive("audit shape")
    for row in entries:
        if not isinstance(row, dict) or row.get("claim") is not True:
            return inconclusive("audit is a claim, not evidence")
    return passed()


async def custody_ack(agent: Agent) -> Verdict:
    """A 202 renews once; the only ack allowed is the message's own final ``processed``."""

    msg_id = str(uuid.uuid4())
    result = await agent.request(
        "POST",
        "/invoke",
        agent.need(body={"service": "lab", "summary": "visit", "district": 8}, msg_id=msg_id),
    )
    if result.status != 202 or result.body.get("status") != "received":
        return failed("received")
    acks = _acks_of(agent, msg_id)
    if _renews_of(agent, msg_id) != [(msg_id, 1)] or acks not in ([], [(msg_id, 1, "processed")]):
        return failed("renew")
    return passed()


async def custody_redelivery(agent: Agent) -> Verdict:
    msg_id = str(uuid.uuid4())
    body = {"service": "lab", "summary": "visit", "district": 8}
    payload = agent.need(body=body, msg_id=msg_id, fence=1)
    await agent.request("POST", "/invoke", payload)
    effects = agent.effect_count()
    result = await agent.request("POST", "/invoke", _with_fence(payload, 2))
    if agent.effect_count() != effects or result.status != 202:
        return failed("second effect")
    if (msg_id, 2) not in agent.renew_log():
        return failed("fence")
    return passed()


async def custody_stale_fence(agent: Agent) -> Verdict:
    msg_id = str(uuid.uuid4())
    body = {"service": "lab", "summary": "visit", "district": 8}
    payload = agent.need(body=body, msg_id=msg_id, fence=1)
    await agent.request("POST", "/invoke", payload)
    await agent.request("POST", "/invoke", _with_fence(payload, 2))
    await agent.settle(msg_id, 2)
    renews = _renews_of(agent, msg_id)
    acks = _acks_of(agent, msg_id)
    await agent.request("POST", "/invoke", _with_fence(payload, 1))
    if _renews_of(agent, msg_id) != renews or _acks_of(agent, msg_id) != acks:
        return failed("stale fence")
    return passed()


def _renews_of(agent: Agent, msg_id: str) -> list[tuple[str, int]]:
    return [row for row in agent.renew_log() if row[0] == msg_id]


def _acks_of(agent: Agent, msg_id: str) -> list[tuple[str, int, str]]:
    return [row for row in agent.ack_log() if row[0] == msg_id]


async def terminal_once(agent: Agent) -> Verdict:
    before = agent.effect_count()
    expired = await agent.request("POST", "/invoke", agent.expired_need())
    if expired.status != 410 or expired.body.get("code") != "EXPIRED":
        return failed("expired")
    if agent.effect_count() != before:
        return failed("expired effect")
    msg_id = str(uuid.uuid4())
    payload = agent.need(body={"service": "lab", "summary": "visit"}, msg_id=msg_id)
    await agent.request("POST", "/invoke", payload)
    await agent.settle(msg_id, 1)
    acks = [row for row in agent.ack_log() if row[0] == msg_id]
    await agent.request("POST", "/invoke", payload)
    again = [row for row in agent.ack_log() if row[0] == msg_id]
    if len(again) != len(acks):
        return failed("second terminal")
    return passed()


async def probe_semantic(agent: Agent) -> Verdict:
    before = agent.effect_count()
    result = await agent.request("POST", "/invoke", agent.probe("n-9"))
    if result.status != 200 or agent.effect_count() != before:
        return failed("brain")
    ack = _signed_ack(agent, result.body)
    if isinstance(ack, str):
        return failed(ack)
    if ack.get("nonce") != "n-9":
        return failed("nonce")
    return passed()


async def errors_signed(agent: Agent) -> Verdict:
    result = await agent.request(
        "POST",
        "/invoke",
        agent.need(body={"service": "lab", "summary": "__error__"}),
    )
    if result.status != 200:
        return failed("status")
    ack = _signed_ack(agent, result.body)
    if isinstance(ack, str):
        return failed(ack)
    if ack.get("status") != "rejected" or ack.get("reason") != "brain":
        return failed("reason")
    return passed()


async def provenance(agent: Agent) -> Verdict:
    card = (await agent.request("GET", "/card")).body
    health = (await agent.request("GET", "/health")).body
    if health.get("build") != card.get("build"):
        return failed("build")
    dependencies = card.get("dependencies")
    models: object = []
    if isinstance(dependencies, dict):
        models = dependencies.get("models", [])
    if health.get("models") != models:
        return failed("models")
    return passed()


async def shutdown(agent: Agent) -> Verdict:
    if getattr(agent, "remote", False) is True:
        return skipped("NOT TESTED in staging")
    payload = agent.need(body={"service": "lab", "summary": "visit", "district": 8})
    msg_id = _msg(payload)
    await agent.request("POST", "/invoke", payload)
    started = time.perf_counter()
    await agent.stop()
    if time.perf_counter() - started >= 8:
        return failed("slow")
    acks = [row for row in agent.ack_log() if row[0] == msg_id]
    if acks != [(msg_id, 1, "retryable")]:
        return failed("retryable")
    return passed()


async def chat_only(agent: Agent) -> Verdict:
    """Under ADR-084 the agent never emits a structured body. City acks are not in the send log."""

    before = len(agent.sent_log())
    payload = agent.need(
        body={"service": "lab", "summary": "visit", "district": 8, "text": "I need a visit"}
    )
    await agent.request("POST", "/invoke", payload)
    for _peer, body, _trace in agent.sent_log()[before:]:
        if set(body) - {"text"}:
            return failed("structured send")
        if not isinstance(body.get("text"), str) or not str(body["text"]):
            return failed("text")
    return passed()


async def later(_agent: Agent) -> Verdict:
    return skipped(_LATER)


async def brain_policy(_agent: Agent) -> Verdict:
    """Team brain policy. Not a deploy gate (ADR-088)."""

    return skipped("team brain policy; not a deploy gate")


def _stage(number: int, name: str, blocking: bool, fn: Any) -> Stage:
    return Stage(number, name, blocking, fn)


CATALOG: tuple[Stage, ...] = (
    _stage(1, "card.resolve", True, card_resolve),
    _stage(2, "card.integrity", True, card_integrity),
    _stage(3, "card.declarations", True, card_declarations),
    _stage(4, "health", True, health),
    _stage(5, "invoke.signature", True, invoke_signature),
    _stage(6, "invoke.schema", True, invoke_schema),
    _stage(7, "invoke.semantics", True, brain_policy),
    _stage(8, "invoke.idempotency", True, invoke_idempotency),
    _stage(9, "hold.lifecycle", True, hold_lifecycle),
    _stage(10, "emergency", True, brain_policy),
    _stage(11, "scope", True, brain_policy),
    _stage(12, "alter", True, alter),
    _stage(13, "disclosure", False, disclosure),
    _stage(14, "faults", False, faults),
    _stage(15, "audit", False, audit),
    _stage(16, "custody.ack", True, custody_ack),
    _stage(17, "custody.redelivery", True, custody_redelivery),
    _stage(18, "custody.stale_fence", True, custody_stale_fence),
    _stage(19, "terminal.exactly_once", True, terminal_once),
    _stage(20, "probe.semantic", True, probe_semantic),
    _stage(21, "errors.signed", True, brain_policy),
    _stage(22, "provenance", True, provenance),
    _stage(23, "shutdown", False, shutdown),
    _stage(24, "recovery", False, later),
    _stage(25, "ingress.auth", True, later),
    _stage(26, "build.attestation", True, later),
    _stage(27, "chat.only", True, chat_only),
)

_NOT_GATED = frozenset({later, hold_lifecycle, brain_policy})

IMPLEMENTED = tuple(stage.number for stage in CATALOG if stage.fn not in _NOT_GATED)

BRAIN_POLICY = (7, 10, 11, 21)
