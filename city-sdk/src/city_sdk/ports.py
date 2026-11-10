"""In-memory stand-ins for the Index, the Relay mailbox, and the Ledger agent (local only)."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

from cap.core.timeutil import format_time

from city_sdk.city import LEDGER, Message, Peer


class HarnessRelay(Protocol):
    acks: list[tuple[str, int, str]]
    renewed: list[tuple[str, int]]
    joined: list[str]

    async def join(self, market_id: str) -> dict[str, object]: ...

    async def post(
        self,
        *,
        to: str,
        message_type: str,
        body: dict[str, object],
        need_id: str | None,
        conversation_id: str | None,
        traceparent: str | None,
    ) -> str: ...

    async def history(self, *, peer: str | None, need_id: str | None) -> list[Message]: ...

    async def ack(self, *, msg_id: str, fence: int, status: str) -> None: ...

    async def renew(self, *, msg_id: str, fence: int) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass
class FixedClock:
    moment: datetime

    def now(self) -> datetime:
        return self.moment

    def advance(self, seconds: float) -> None:
        self.moment = self.moment + timedelta(seconds=seconds)


@dataclass
class MemoryIndex:
    """Local search. It does not read the query: every peer comes back, as the city would
    return every agent of the families a query names."""

    peers: list[Peer] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)

    async def search(self, query: str) -> list[Peer]:
        self.queries.append(query)
        return list(self.peers)


class LocalLedger:
    """A local stand-in for the ``city:ledger`` agent. It answers ``help``, ``world``,
    ``hold``, ``commit``, and ``prescription`` in the city's answer shape; the city Ledger
    reads every operation of ``ledger.yaml``."""

    def __init__(self, slots: list[dict[str, Any]] | None = None) -> None:
        self.slots: list[dict[str, Any]] = [] if slots is None else slots
        self.requests: list[str] = []
        self.holds: dict[str, dict[str, Any]] = {}
        self.prescriptions: dict[str, dict[str, Any]] = {}
        self._n = 0

    def issue(self, prescription_number: str, procedure: str, priority: str = "B") -> None:
        self.prescriptions[prescription_number] = {
            "prescription_number": prescription_number,
            "procedure": procedure,
            "priority": priority,
            "state": "issued",
        }

    def answer(self, sender: str, text: str) -> str:
        self.requests.append(text)
        words = text.split()
        op = words[0].lower() if words else ""
        fields = dict(word.split("=", 1) for word in words[1:] if "=" in word)
        if op == "world":
            rows = [
                " ".join(f"{key}={slot[key]}" for key in sorted(slot) if _plain(slot[key]))
                for slot in self.slots[:10]
            ]
            return "\n".join([f"world slots={len(self.slots)}", *rows])[:2000]
        if op == "hold":
            slot_id = fields.get("slot_id", "")
            if not slot_id:
                return "hold needs slot_id=. Nothing was written."
            hold_id = self._ref("bk")
            self.holds[hold_id] = {"slot_id": slot_id, "by": sender}
            return f"held hold_id={hold_id} slot_id={slot_id} fence=1"
        if op == "prescription":
            number = fields.get("prescription_number", "")
            found = self.prescriptions.get(number)
            if found is None:
                return f"prescription prescription_number={number or '?'} not found"
            return "prescription " + " ".join(f"{key}={found[key]}" for key in sorted(found))
        if op == "commit":
            hold_id = fields.get("hold_id", "")
            if not hold_id or hold_id not in self.holds:
                return "commit needs the hold_id= of a hold. Nothing was written."
            ref = self._ref("c")
            return f"committed booking_id={hold_id} ledger_ref={ref} receipt=local"
        return (
            "local ledger: I read help, world, hold slot_id=, commit hold_id=, "
            "prescription prescription_number=. "
            "The city Ledger reads every operation."
        )

    def _ref(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n}"


def _plain(value: object) -> bool:
    return isinstance(value, (str, int, float, bool))


@dataclass
class MemoryRelay:
    """The mailbox in memory. ``replies`` holds the stand-in Ledger's answers for the local
    runner to deliver back as inbound messages."""

    agent_id: str = ""
    acks: list[tuple[str, int, str]] = field(default_factory=list)
    renewed: list[tuple[str, int]] = field(default_factory=list)
    joined: list[str] = field(default_factory=list)
    posted: list[dict[str, Any]] = field(default_factory=list)
    log: list[Message] = field(default_factory=list)
    replies: list[Message] = field(default_factory=list)
    ledger: LocalLedger | None = None

    async def join(self, market_id: str) -> dict[str, object]:
        self.joined.append(market_id)
        return {"session": "local", "epoch": 0}

    async def post(
        self,
        *,
        to: str,
        message_type: str,
        body: dict[str, object],
        need_id: str | None,
        conversation_id: str | None,
        traceparent: str | None,
    ) -> str:
        msg_id = str(uuid4())
        self.posted.append(
            {
                "msg_id": msg_id,
                "to": to,
                "type": message_type,
                "body": copy.deepcopy(body),
                "need_id": need_id,
                "conversation_id": conversation_id,
                "traceparent": traceparent,
            }
        )
        self.record(
            Message(
                type=message_type,
                sender=self.agent_id,
                recipient=to,
                msg_id=msg_id,
                need_id=need_id,
                conversation_id=conversation_id,
                body=body,
                sent_at=format_time(datetime.now(UTC)),
            )
        )
        text = body.get("text")
        if to == LEDGER and self.ledger is not None and isinstance(text, str):
            answer = Message(
                type="chat",
                sender=LEDGER,
                recipient=self.agent_id,
                msg_id=str(uuid4()),
                need_id=need_id,
                conversation_id=conversation_id,
                body={"text": self.ledger.answer(self.agent_id, text)},
                sent_at=format_time(datetime.now(UTC)),
            )
            self.replies.append(answer)
        return msg_id

    def record(self, message: Message) -> None:
        if all(row.msg_id != message.msg_id for row in self.log):
            self.log.append(message)

    async def history(self, *, peer: str | None, need_id: str | None) -> list[Message]:
        return [
            row
            for row in self.log
            if (peer is None or peer in (row.sender, row.recipient))
            and (need_id is None or row.need_id == need_id)
        ]

    async def ack(self, *, msg_id: str, fence: int, status: str) -> None:
        self.acks.append((msg_id, fence, status))

    async def renew(self, *, msg_id: str, fence: int) -> None:
        self.renewed.append((msg_id, fence))
