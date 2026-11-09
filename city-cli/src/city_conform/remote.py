"""Conformance against an agent URL.

Effects that the HTTP transcript shows are counted from that transcript.
Acks, renews, sends, holds, memory, and escalations come from the city
that recorded them. An empty stand-in is not a pass.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

import httpx
from cap.core.envelope import build, sign
from cap.core.keys import PublicKey
from cap.core.timeutil import format_time
from city.testkit import ephemeral
from city_sdk.city import role_for
from city_sdk.http import HttpIndex
from city_sdk.ports import Clock, SystemClock

from city_conform.agent import HttpResult, Sender, SuiteSigners
from city_conform.runner import Report, Stage, run_profile

AGENT_CARD = "/card"
_EVIDENCE_PATH = "/v1/evidence"
_PRIVATE_AGENT_SUFFIX = ".agents.internal"
# The ingress can stall the first read of a new pod. Five seconds is httpx's default,
# and that stall was failing the release as card.resolve: ReadTimeout.
_AGENT_TIMEOUT = httpx.Timeout(20.0, connect=5.0)


class MissingCityEvidence(Exception):
    """The city did not supply an observation. Silence is not an empty log."""


class CityEvidence(Protocol):
    async def load(self) -> None: ...

    async def aclose(self) -> None: ...

    def acks(self) -> list[tuple[str, int, str]]: ...

    def renews(self) -> list[tuple[str, int]]: ...

    def sent(self) -> list[tuple[str, dict[str, object], str | None]]: ...

    def holds(self) -> list[tuple[str, str]]: ...

    def memory(self) -> dict[str, str]: ...

    def escalations(self) -> list[str]: ...


class DeliveryOpener(Protocol):
    async def __call__(self, payload: Mapping[str, Any]) -> None: ...


class IndexView(Protocol):
    """The live Index as the agent's own caller sees it."""

    async def find(self, *, role: str) -> Sequence[Any]: ...


class _RelayView(Protocol):
    acks: list[tuple[str, int, str]]
    renewed: list[tuple[str, int]]
    sent: list[tuple[str, dict[str, object], str | None]]


class _LedgerView(Protocol):
    calls: list[tuple[str, str]]


class PortCityEvidence:
    """Reads the relay, ledger, and escalation list the harness already calls."""

    def __init__(
        self,
        relay: _RelayView,
        ledger: _LedgerView,
        memory: dict[str, str],
        escalations: list[str],
    ) -> None:
        self._relay = relay
        self._ledger = ledger
        self._memory = memory
        self._escalations = escalations

    async def load(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    def acks(self) -> list[tuple[str, int, str]]:
        return list(self._relay.acks)

    def renews(self) -> list[tuple[str, int]]:
        return list(self._relay.renewed)

    def sent(self) -> list[tuple[str, dict[str, object], str | None]]:
        return list(self._relay.sent)

    def holds(self) -> list[tuple[str, str]]:
        return list(self._ledger.calls)

    def memory(self) -> dict[str, str]:
        return dict(self._memory)

    def escalations(self) -> list[str]:
        return list(self._escalations)


class HttpCityEvidence:
    """GET ``/v1/evidence`` from the city that observed the agent."""

    def __init__(
        self,
        url: str,
        *,
        agent_id: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._agent_id = agent_id
        self._owns = client is None
        self._client = client or httpx.AsyncClient(base_url=url, timeout=_AGENT_TIMEOUT)
        self._acks: list[tuple[str, int, str]] = []
        self._renews: list[tuple[str, int]] = []
        self._sent: list[tuple[str, dict[str, object], str | None]] = []
        self._holds: list[tuple[str, str]] = []
        self._memory: dict[str, str] = {}
        self._escalations: list[str] = []

    async def load(self) -> None:
        response = await _call(
            self._client, "GET", _EVIDENCE_PATH, params={"agent_id": self._agent_id}
        )
        if response.status_code != 200:
            raise MissingCityEvidence("http")
        body = response.json()
        if not isinstance(body, dict):
            raise MissingCityEvidence("http")
        self._acks = _ack_rows(body.get("acks"))
        self._renews = _renew_rows(body.get("renews"))
        self._sent = _sent_rows(body.get("sent"))
        self._holds = _hold_rows(body.get("holds"))
        self._memory = _memory(body.get("memory"))
        self._escalations = _strings(body.get("escalations"))

    async def aclose(self) -> None:
        if self._owns:
            await self._client.aclose()

    def acks(self) -> list[tuple[str, int, str]]:
        return list(self._acks)

    def renews(self) -> list[tuple[str, int]]:
        return list(self._renews)

    def sent(self) -> list[tuple[str, dict[str, object], str | None]]:
        return list(self._sent)

    def holds(self) -> list[tuple[str, str]]:
        return list(self._holds)

    def memory(self) -> dict[str, str]:
        return dict(self._memory)

    def escalations(self) -> list[str]:
        return list(self._escalations)


class RemoteAgent:
    """Drives ``/health``, ``/card``, and ``/invoke`` on a running agent."""

    remote = True

    def __init__(
        self,
        base_url: str,
        *,
        agent_id: str,
        public: PublicKey,
        client: httpx.AsyncClient,
        clock: Clock | None = None,
        evidence: CityEvidence | None = None,
        signers: SuiteSigners | None = None,
        index: IndexView | None = None,
        deliveries: DeliveryOpener | None = None,
        settle_wait: float = 5.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.agent_id = agent_id
        self._public = public
        self._client = client
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._evidence = evidence
        self._index = index
        self._deliveries = deliveries
        self._settle_wait = settle_wait
        self._business: set[str] = set()
        self._hidden = 0
        self._escalation: str | None = None
        if signers is None:
            sender = ephemeral()
            prober = ephemeral()
            city = ephemeral()
            stranger = ephemeral()
            self._sender: Sender = ("agent:t05/osp-x", sender.private)
            self._prober: Sender = ("city:prober", prober.private)
            self._city: Sender = ("city:governance", city.private)
            self._stranger: Sender = ("agent:t99/stranger", stranger.private)
        else:
            self._sender = signers.sender
            self._prober = signers.prober
            self._city = signers.city
            self._stranger = signers.stranger

    def now(self) -> datetime:
        return self._clock.now()

    def public_key(self) -> PublicKey:
        return self._public

    def effect_count(self) -> int:
        return len(self._business) + self._hidden

    def ack_log(self) -> list[tuple[str, int, str]]:
        return self._evidence_or_raise().acks()

    def renew_log(self) -> list[tuple[str, int]]:
        return self._evidence_or_raise().renews()

    def sent_log(self) -> list[tuple[str, dict[str, object], str | None]]:
        return self._evidence_or_raise().sent()

    def hold_log(self) -> list[tuple[str, str]]:
        return self._evidence_or_raise().holds()

    def memory_view(self) -> dict[str, str]:
        return self._evidence_or_raise().memory()

    def escalation(self) -> str | None:
        self._evidence_or_raise()
        return self._escalation

    async def stop(self) -> None:
        return None

    async def settle(self, msg_id: str, fence: int) -> None:
        """Wait until the city records the agent's final ack for ``fence``, or give up."""

        if self._evidence is None:
            return
        deadline = time.monotonic() + self._settle_wait
        while True:
            await self._evidence.load()
            if any(row[0] == msg_id and row[1] == fence for row in self._evidence.acks()):
                return
            if time.monotonic() >= deadline:
                return
            await asyncio.sleep(0.1)

    async def wait_renew(self, msg_id: str, fence: int) -> None:
        """Wait until the city records a renew for ``msg_id`` at ``fence``, or give up."""

        if self._evidence is None:
            return
        deadline = time.monotonic() + self._settle_wait
        while True:
            await self._evidence.load()
            if any(row[0] == msg_id and row[1] == fence for row in self._evidence.renews()):
                return
            if time.monotonic() >= deadline:
                return
            await asyncio.sleep(0.1)

    async def index_peers(self, *, service: str, district: int | None) -> list[str] | None:
        del district
        role = role_for(service)
        if self._index is None or role is None:
            return None
        found = await self._index.find(role=role)
        return [str(peer.agent_id) for peer in found]

    async def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> HttpResult:
        before_activity = self._activity()
        before_escalations = self._escalation_count()
        if path == "/invoke" and payload is not None and self._deliveries is not None:
            await self._deliveries(payload)
        result = await self._exchange(method, path, payload)
        if path == "/invoke":
            await self._observe(payload, result, before_activity, before_escalations)
        return result

    def need(
        self,
        *,
        body: dict[str, Any],
        msg_id: str | None = None,
        fence: int = 1,
        expires_at: datetime | None = None,
    ) -> dict[str, Any]:
        return self._delivery(
            self._sender,
            "need.request",
            body,
            msg_id=msg_id,
            fence=fence,
            expires_at=expires_at,
        )

    def unsigned_need(self) -> dict[str, Any]:
        msg_id = str(uuid.uuid4())
        envelope = build(
            msg_id=msg_id,
            message_type="need.request",
            sender=self._sender[0],
            recipient=self.agent_id,
            sent_at=self._clock.now(),
            body={"service": "lab", "summary": "visit"},
        )
        return _wrap(msg_id, envelope, fence=1, now=self._clock)

    def bad_signature_need(self) -> dict[str, Any]:
        payload = self.need(body={"service": "lab", "summary": "visit"})
        envelope = payload["envelope"]
        if isinstance(envelope, dict):
            sig = dict(cast(dict[str, Any], envelope.get("sig", {})))
            token = str(sig.get("jws", "x"))
            sig["jws"] = ("A" if token[:1] != "A" else "B") + token[1:]
            envelope["sig"] = sig
        return payload

    def unknown_key_need(self) -> dict[str, Any]:
        return self._delivery(
            self._stranger,
            "need.request",
            {"service": "lab", "summary": "visit"},
        )

    def expired_need(self) -> dict[str, Any]:
        return self.need(
            body={"service": "lab", "summary": "visit"},
            expires_at=self._clock.now() - timedelta(seconds=5),
        )

    def probe(self, nonce: str) -> dict[str, Any]:
        return self._delivery(self._prober, "cap.probe", {"nonce": nonce})

    def order(self, *, epoch: int, kind: str, sender: Sender | None = None) -> dict[str, Any]:
        who = self._city if sender is None else sender
        msg_id = str(uuid.uuid4())
        envelope = sign(
            build(
                msg_id=msg_id,
                message_type="city.order",
                sender=who[0],
                recipient=self.agent_id,
                sent_at=self._clock.now(),
                body={
                    "order_id": f"ord:{epoch}:001",
                    "epoch": epoch,
                    "kind": kind,
                    "requires_ack": True,
                },
            ),
            who[1],
            kid=who[0] + "#k1",
        )
        return cast(dict[str, Any], envelope)

    def foreign_order(self) -> dict[str, Any]:
        return self.order(epoch=1, kind="ping", sender=self._sender)

    def _evidence_or_raise(self) -> CityEvidence:
        if self._evidence is None:
            raise MissingCityEvidence("city")
        return self._evidence

    def _activity(self) -> int:
        if self._evidence is None:
            return 0
        evidence = self._evidence
        return (
            len(evidence.sent())
            + len(evidence.holds())
            + len(evidence.escalations())
            + len(evidence.memory())
        )

    def _escalation_count(self) -> int:
        if self._evidence is None:
            return 0
        return len(self._evidence.escalations())

    async def _exchange(self, method: str, path: str, payload: dict[str, Any] | None) -> HttpResult:
        response = await _call(self._client, method, path, json_body=payload)
        body: dict[str, Any] = {}
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            body = parsed
        return HttpResult(response.status_code, body)

    async def _observe(
        self,
        payload: dict[str, Any] | None,
        result: HttpResult,
        before_activity: int,
        before_escalations: int,
    ) -> None:
        if self._evidence is not None:
            await self._evidence.load()
            grown = self._activity() - before_activity
            after = self._evidence.escalations()
            if len(after) > before_escalations:
                self._escalation = after[-1]
            else:
                self._escalation = None
            # A 202 renew is written by the Relay after the agent replies. Poll so
            # custody.ack does not read an empty renew log on a cold first release.
            if result.status == 202 and result.body.get("status") == "received":
                msg_id = _delivery_id(payload) if payload is not None else None
                fence = _delivery_fence(payload) if payload is not None else None
                if msg_id is not None and fence is not None:
                    await self.wait_renew(msg_id, fence)
        else:
            grown = 0
        accepted = _accepted(payload, result)
        if accepted is not None:
            self._business.add(accepted)
            return
        if grown > 0:
            self._hidden += grown

    def _delivery(
        self,
        sender: Sender,
        message_type: str,
        body: dict[str, Any],
        *,
        msg_id: str | None = None,
        fence: int = 1,
        expires_at: datetime | None = None,
    ) -> dict[str, Any]:
        chosen = msg_id if msg_id is not None else str(uuid.uuid4())
        envelope = sign(
            build(
                msg_id=chosen,
                message_type=message_type,
                sender=sender[0],
                recipient=self.agent_id,
                sent_at=self._clock.now(),
                body=body,
                expires_at=expires_at,
            ),
            sender[1],
            kid=sender[0] + "#k1",
        )
        return _wrap(chosen, cast(dict[str, Any], envelope), fence=fence, now=self._clock)


async def _call(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    params: Mapping[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> httpx.Response:
    """Send one request. A GET is tried twice after a timeout.

    The agent ingress sometimes accepts the connection and then never writes
    the first response. A second GET reaches the pod. A POST is not repeated:
    the pod may already have applied it.
    """

    attempts = 2 if method == "GET" else 1
    last: httpx.TimeoutException | None = None
    for _attempt in range(attempts):
        try:
            if method == "GET":
                return await client.get(path, params=params)
            return await client.post(path, json=json_body)
        except httpx.TimeoutException as exc:
            last = exc
    assert last is not None
    raise last


async def _read_card(client: httpx.AsyncClient, *, wait: float, pause: float) -> dict[str, Any]:
    """Read the card until it arrives as a JSON object, for at most ``wait`` seconds.

    Right after a rollout a load balancer proxy without the new route answers with its
    own page. That page is not a card, and not yet the agent's answer.
    """

    deadline = time.monotonic() + wait
    while True:
        response = await _call(client, "GET", AGENT_CARD)
        body = _json_object(response) if response.status_code == 200 else None
        if body is not None:
            return body
        if time.monotonic() >= deadline:
            raise ValueError(f"card: HTTP {response.status_code}")
        await asyncio.sleep(pause)


def _json_object(response: httpx.Response) -> dict[str, Any] | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _tls_verify(url: str) -> bool | str:
    """Public hosts use system trust. Private agent ingress uses CITY_AGENT_CA_FILE."""

    host = (urlsplit(url).hostname or "").lower()
    if not host.endswith(_PRIVATE_AGENT_SUFFIX):
        return True
    path = os.environ.get("CITY_AGENT_CA_FILE", "").strip()
    return path if path else False


async def conform_url(
    url: str,
    *,
    stages: Sequence[Stage],
    transport: httpx.AsyncBaseTransport | None = None,
    client: httpx.AsyncClient | None = None,
    evidence: CityEvidence | None = None,
    signers: SuiteSigners | None = None,
    index_url: str | None = None,
    deliveries: DeliveryOpener | None = None,
    card_wait: float = 30.0,
    card_pause: float = 1.0,
    settle_wait: float = 5.0,
) -> Report:
    owns = client is None
    http = client or httpx.AsyncClient(
        transport=transport, base_url=url, verify=_tls_verify(url), timeout=_AGENT_TIMEOUT
    )
    index_http: httpx.AsyncClient | None = None
    try:
        card_body = await _read_card(http, wait=card_wait, pause=card_pause)
        agent_id = card_body.get("agent_id")
        keys = card_body.get("keys")
        if not isinstance(agent_id, str) or not isinstance(keys, list) or not keys:
            raise ValueError("card")
        key = keys[0]
        if not isinstance(key, dict):
            raise ValueError("card")
        public = PublicKey.from_jwk(key)
        if evidence is not None:
            await evidence.load()
        index: IndexView | None = None
        if index_url:
            index_http = httpx.AsyncClient(base_url=index_url.rstrip("/"), timeout=_AGENT_TIMEOUT)
            index = HttpIndex(caller=agent_id, client=index_http)
        agent = RemoteAgent(
            url,
            agent_id=agent_id,
            public=public,
            client=http,
            evidence=evidence,
            signers=signers,
            index=index,
            deliveries=deliveries,
            settle_wait=settle_wait,
        )
        return await run_profile(agent, stages)
    finally:
        if index_http is not None:
            await index_http.aclose()
        if evidence is not None:
            await evidence.aclose()
        if owns:
            await http.aclose()


def _wrap(msg_id: str, envelope: dict[str, Any], *, fence: int, now: Clock) -> dict[str, Any]:
    return {
        "delivery": {
            "msg_id": msg_id,
            "mailbox": "m-1",
            "fence": fence,
            "attempt": 1,
            "lease_until": format_time(now.now()),
        },
        "envelope": envelope,
    }


def _accepted(payload: dict[str, Any] | None, result: HttpResult) -> str | None:
    if payload is None or _probe_result(result.body):
        return None
    status = result.body.get("status")
    if result.status == 202 and status == "received":
        return _delivery_id(payload)
    if result.status == 200 and status in {"processed", "rejected"}:
        return _delivery_id(payload)
    return None


def _probe_result(body: dict[str, Any]) -> bool:
    envelopes = body.get("envelopes")
    if not isinstance(envelopes, list) or not envelopes:
        return False
    first = envelopes[0]
    return isinstance(first, dict) and first.get("type") == "cap.probe.result"


def _delivery_id(payload: dict[str, Any]) -> str | None:
    delivery = payload.get("delivery")
    if not isinstance(delivery, dict):
        return None
    msg_id = delivery.get("msg_id")
    if not isinstance(msg_id, str) or not msg_id:
        return None
    return msg_id


def _delivery_fence(payload: dict[str, Any]) -> int | None:
    delivery = payload.get("delivery")
    if not isinstance(delivery, dict):
        return None
    fence = delivery.get("fence")
    if type(fence) is not int or fence < 1:
        return None
    return fence


def _rows(value: object) -> list[object]:
    if not isinstance(value, list):
        raise MissingCityEvidence("shape")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise MissingCityEvidence("shape")
    return value


def _whole(value: object) -> int:
    if type(value) is not int:
        raise MissingCityEvidence("shape")
    return value


def _ack_rows(value: object) -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    for row in _rows(value):
        if not isinstance(row, list) or len(row) != 3:
            raise MissingCityEvidence("shape")
        found.append((_text(row[0]), _whole(row[1]), _text(row[2])))
    return found


def _renew_rows(value: object) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for row in _rows(value):
        if not isinstance(row, list) or len(row) != 2:
            raise MissingCityEvidence("shape")
        found.append((_text(row[0]), _whole(row[1])))
    return found


def _sent_rows(value: object) -> list[tuple[str, dict[str, object], str | None]]:
    found: list[tuple[str, dict[str, object], str | None]] = []
    for row in _rows(value):
        if not isinstance(row, list) or len(row) != 3 or not isinstance(row[1], dict):
            raise MissingCityEvidence("shape")
        trace = row[2]
        if trace is not None and not isinstance(trace, str):
            raise MissingCityEvidence("shape")
        body = {str(key): item for key, item in row[1].items()}
        found.append((_text(row[0]), body, trace))
    return found


def _hold_rows(value: object) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for row in _rows(value):
        if not isinstance(row, list) or len(row) != 2:
            raise MissingCityEvidence("shape")
        found.append((_text(row[0]), _text(row[1])))
    return found


def _memory(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise MissingCityEvidence("shape")
    found: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise MissingCityEvidence("shape")
        found[key] = item
    return found


def _strings(value: object) -> list[str]:
    return [_text(item) for item in _rows(value)]
