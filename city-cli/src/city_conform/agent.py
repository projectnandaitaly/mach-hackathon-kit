"""Agent under test. The live agent is the harness behind ASGI."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, cast

from cap.core.envelope import build, sign
from cap.core.keys import PrivateKey, PublicKey
from cap.core.timeutil import format_time
from city_sdk.app import create_app
from city_sdk.harness import Harness
from httpx import ASGITransport, AsyncClient

Sender = tuple[str, PrivateKey]


@dataclass(frozen=True)
class SuiteSigners:
    """City and peer keys the agent already trusts. The runner does not invent them."""

    sender: Sender
    prober: Sender
    city: Sender
    stranger: Sender


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: dict[str, Any]


class Agent(Protocol):
    def now(self) -> datetime: ...

    def public_key(self) -> PublicKey: ...

    def effect_count(self) -> int: ...

    def ack_log(self) -> list[tuple[str, int, str]]: ...

    def renew_log(self) -> list[tuple[str, int]]: ...

    def sent_log(self) -> list[tuple[str, dict[str, object], str | None]]: ...

    def hold_log(self) -> list[tuple[str, str]]: ...

    def memory_view(self) -> dict[str, str]: ...

    def escalation(self) -> str | None: ...

    async def stop(self) -> None: ...

    async def settle(self, msg_id: str, fence: int) -> None: ...

    async def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> HttpResult: ...

    def need(
        self,
        *,
        body: dict[str, Any],
        msg_id: str | None = None,
        fence: int = 1,
        expires_at: datetime | None = None,
    ) -> dict[str, Any]: ...

    def unsigned_need(self) -> dict[str, Any]: ...

    def bad_signature_need(self) -> dict[str, Any]: ...

    def unknown_key_need(self) -> dict[str, Any]: ...

    def expired_need(self) -> dict[str, Any]: ...

    def probe(self, nonce: str) -> dict[str, Any]: ...

    def order(self, *, epoch: int, kind: str, sender: Sender | None = None) -> dict[str, Any]: ...

    def foreign_order(self) -> dict[str, Any]: ...


def _tamper(token: str) -> str:
    first = token[0]
    replacement = "A" if first != "A" else "B"
    return replacement + token[1:]


class LiveAgent:
    def __init__(
        self,
        harness: Harness,
        *,
        sender: Sender,
        prober: Sender,
        city: Sender,
        stranger: Sender,
    ) -> None:
        self.harness = harness
        self._sender = sender
        self._prober = prober
        self._city = city
        self._stranger = stranger
        self._app = create_app(harness)

    def now(self) -> datetime:
        return self.harness.clock.now()

    def public_key(self) -> PublicKey:
        return self.harness.public

    def effect_count(self) -> int:
        return self.harness.brain_calls

    def ack_log(self) -> list[tuple[str, int, str]]:
        return list(self.harness.relay.acks)

    def renew_log(self) -> list[tuple[str, int]]:
        return list(self.harness.relay.renewed)

    def sent_log(self) -> list[tuple[str, dict[str, object], str | None]]:
        return list(self.harness.relay.sent)

    def hold_log(self) -> list[tuple[str, str]]:
        return list(self.harness.ledger.calls)

    def memory_view(self) -> dict[str, str]:
        return dict(self.harness.memory)

    def escalation(self) -> str | None:
        market = self.harness.last_market
        if market is None:
            return None
        return market.escalated

    async def stop(self) -> None:
        await self.harness.shutdown()

    async def settle(self, msg_id: str, fence: int) -> None:
        del msg_id, fence
        await self.harness.flush_acks()

    async def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> HttpResult:
        async with AsyncClient(
            transport=ASGITransport(app=self._app),
            base_url="http://agent",
        ) as client:
            if method == "GET":
                response = await client.get(path)
            else:
                response = await client.post(path, json=payload)
        body: dict[str, Any] = {}
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            body = parsed
        return HttpResult(response.status_code, body)

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
            recipient=self.harness.agent_id,
            sent_at=self.now(),
            body={"service": "lab", "summary": "visit"},
        )
        return self._wrap(msg_id, envelope, fence=1)

    def bad_signature_need(self) -> dict[str, Any]:
        payload = self.need(body={"service": "lab", "summary": "visit"})
        envelope = payload["envelope"]
        sig = dict(envelope["sig"])
        sig["jws"] = _tamper(str(sig["jws"]))
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
            expires_at=self.now() - timedelta(seconds=5),
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
                recipient=self.harness.agent_id,
                sent_at=self.now(),
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

    def suite_signers(self) -> SuiteSigners:
        return SuiteSigners(
            sender=self._sender,
            prober=self._prober,
            city=self._city,
            stranger=self._stranger,
        )

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
                recipient=self.harness.agent_id,
                sent_at=self.now(),
                body=body,
                expires_at=expires_at,
            ),
            sender[1],
            kid=sender[0] + "#k1",
        )
        return self._wrap(chosen, envelope, fence=fence)

    def _wrap(self, msg_id: str, envelope: dict[str, Any], *, fence: int) -> dict[str, Any]:
        return {
            "delivery": {
                "msg_id": msg_id,
                "mailbox": "m-1",
                "fence": fence,
                "attempt": 1,
                "lease_until": format_time(self.now()),
            },
            "envelope": envelope,
        }
