"""In-memory stand-ins for Index, Relay, ledger, care registers, and attestation."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

from cap.core.errors import CapError
from cap.core.timeutil import format_time, parse_time

from city_sdk.market import Peer, role_for


class HarnessRelay(Protocol):
    acks: list[tuple[str, int, str]]
    sent: list[tuple[str, dict[str, object], str | None]]
    renewed: list[tuple[str, int]]
    joined: list[str]

    async def join(self, market_id: str) -> dict[str, object]: ...

    async def send(
        self,
        agent_id: str,
        body: dict[str, object],
        *,
        traceparent: str | None,
    ) -> None: ...

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

    async def ack(self, *, msg_id: str, fence: int, status: str) -> None: ...

    async def renew(self, *, msg_id: str, fence: int) -> None: ...


class HarnessLedger(Protocol):
    calls: list[tuple[str, str]]

    async def hold(self, agent_id: str) -> str: ...

    async def commit(self, hold_id: str) -> None: ...

    async def release(self, hold_id: str) -> None: ...


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
    peers: list[Peer] = field(default_factory=list)

    async def find(self, *, role: str) -> list[Peer]:
        """Peers of ``role``. A peer without a role takes the role of its service."""

        return [peer for peer in self.peers if _role_of(peer) in ("", role)]


def _role_of(peer: Peer) -> str:
    if peer.role:
        return peer.role
    return (role_for(peer.service) or "b2b") if peer.service else ""


@dataclass
class MemoryRelay:
    acks: list[tuple[str, int, str]] = field(default_factory=list)
    sent: list[tuple[str, dict[str, object], str | None]] = field(default_factory=list)
    renewed: list[tuple[str, int]] = field(default_factory=list)
    joined: list[str] = field(default_factory=list)
    posted: list[dict[str, Any]] = field(default_factory=list)

    async def join(self, market_id: str) -> dict[str, object]:
        self.joined.append(market_id)
        return {"session": "local", "epoch": 0}

    async def send(
        self,
        agent_id: str,
        body: dict[str, object],
        *,
        traceparent: str | None,
    ) -> None:
        self.sent.append((agent_id, body, traceparent))

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
        return msg_id

    async def ack(self, *, msg_id: str, fence: int, status: str) -> None:
        self.acks.append((msg_id, fence, status))

    async def renew(self, *, msg_id: str, fence: int) -> None:
        self.renewed.append((msg_id, fence))


@dataclass
class MemoryLedger:
    calls: list[tuple[str, str]] = field(default_factory=list)
    _n: int = 0

    async def hold(self, agent_id: str) -> str:
        self._n += 1
        hold_id = f"hold-{self._n}"
        self.calls.append(("hold", agent_id))
        return hold_id

    async def commit(self, hold_id: str) -> None:
        self.calls.append(("commit", hold_id))

    async def release(self, hold_id: str) -> None:
        self.calls.append(("release", hold_id))


@dataclass
class MemoryAttest:
    ratings: list[tuple[str, int]] = field(default_factory=list)

    async def rate(self, agent_id: str, score: int) -> None:
        self.ratings.append((agent_id, score))


class MemoryCare:
    """Ledger care registers in memory. Response shapes follow ledger.yaml."""

    def __init__(
        self,
        *,
        world: dict[str, Any] | None = None,
        agenda: dict[str, Any] | None = None,
        rules: list[dict[str, Any]] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._world = world if world is not None else {"wave_id": "local", "world": {}}
        self._agenda = agenda
        self._rules = [] if rules is None else rules
        self._clock: Clock = clock if clock is not None else SystemClock()
        self.prescriptions: dict[str, dict[str, Any]] = {}
        self.holds: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, Any]] = []
        self._n = 0

    async def world(
        self, from_date: date | None = None, until_date: date | None = None
    ) -> dict[str, Any]:
        """The whole local world, or only its slots of ``[from_date, until_date)``."""

        found = copy.deepcopy(self._world)
        if from_date is None and until_date is None:
            return found
        node = found.get("world")
        if isinstance(node, dict) and isinstance(node.get("slots"), list):
            node["slots"] = [
                slot
                for slot in node["slots"]
                if _in_window(str(slot.get("agenda_date", "")), from_date, until_date)
            ]
        return found

    async def agenda_now(self) -> dict[str, Any]:
        if self._agenda is not None:
            return dict(self._agenda)
        now = format_time(self._clock.now())
        return {
            "wave_id": str(self._world.get("wave_id", "local")),
            "agenda_now": now,
            "agenda_day": self._clock.now().strftime("%a").lower(),
            "epoch_real": now,
            "epoch_agenda": now,
            "rate": 1,
            "paused": False,
        }

    async def rules(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._rules)

    async def issue_prescription(self, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("prescribe", copy.deepcopy(body)))
        prescription_number = str(body["prescription_number"])
        if prescription_number in self.prescriptions:
            raise CapError("DUPLICATE", prescription_number)
        record = {
            "prescription_number": prescription_number,
            "state": "issued",
            "ledger_ref": self._ref("p"),
            "prescription": copy.deepcopy(body),
        }
        self.prescriptions[prescription_number] = record
        return copy.deepcopy(record)

    async def prescription(self, prescription_number: str) -> dict[str, Any]:
        self.calls.append(("prescription", prescription_number))
        try:
            return copy.deepcopy(self.prescriptions[prescription_number])
        except KeyError as exc:
            raise CapError("TARGET_NOT_VISIBLE", prescription_number) from exc

    async def checkin(self, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("checkin", copy.deepcopy(body)))
        agenda = body.get("agenda_time") or (await self.agenda_now())["agenda_now"]
        return {
            "checkin_id": self._ref("ck"),
            "booking_id": body["booking_id"],
            "ledger_ref": self._ref("c"),
            "agenda_time": agenda,
        }

    async def report(self, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("report", copy.deepcopy(body)))
        record = {
            "report_id": self._ref("rp"),
            "booking_id": body["booking_id"],
            "ledger_ref": self._ref("r"),
        }
        if "follow_up_prescription_number" in body:
            record["follow_up_prescription_number"] = body["follow_up_prescription_number"]
        return record

    async def hold_slot(self, facility_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("hold_slot", {"facility_id": facility_id, "body": copy.deepcopy(body)}))
        hold_id = self._ref("bk")
        self.holds[hold_id] = copy.deepcopy(body)
        return {"hold_id": hold_id, "fence": body.get("fence", 1)}

    async def commit_slot(self, hold_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("commit_slot", {"hold_id": hold_id, "body": copy.deepcopy(body)}))
        held = self.holds.get(hold_id)
        if held is None:
            raise CapError("TARGET_NOT_VISIBLE", hold_id)
        ledger_ref = self._ref("c")
        receipt: dict[str, Any] = {
            "ledger_ref": ledger_ref,
            "booking_id": hold_id,
            "slot_id": held["slot_id"],
            "signature": "memory",
        }
        prescription_number = body.get("prescription_number", held.get("prescription_number"))
        if prescription_number is not None:
            receipt["prescription_number"] = prescription_number
        return {"booking_id": hold_id, "ledger_ref": ledger_ref, "receipt": receipt}

    def _ref(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n}"


class ClosedCare:
    """Fail closed when no Ledger is wired."""

    async def world(
        self, from_date: date | None = None, until_date: date | None = None
    ) -> dict[str, Any]:
        del from_date, until_date
        raise CapError("POLICY_VIOLATION", "ledger")

    async def agenda_now(self) -> dict[str, Any]:
        raise CapError("POLICY_VIOLATION", "ledger")

    async def rules(self) -> list[dict[str, Any]]:
        raise CapError("POLICY_VIOLATION", "ledger")

    async def issue_prescription(self, body: dict[str, Any]) -> dict[str, Any]:
        del body
        raise CapError("POLICY_VIOLATION", "ledger")

    async def prescription(self, prescription_number: str) -> dict[str, Any]:
        del prescription_number
        raise CapError("POLICY_VIOLATION", "ledger")

    async def checkin(self, body: dict[str, Any]) -> dict[str, Any]:
        del body
        raise CapError("POLICY_VIOLATION", "ledger")

    async def report(self, body: dict[str, Any]) -> dict[str, Any]:
        del body
        raise CapError("POLICY_VIOLATION", "ledger")

    async def hold_slot(self, facility_id: str, body: dict[str, Any]) -> dict[str, Any]:
        del facility_id, body
        raise CapError("POLICY_VIOLATION", "ledger")

    async def commit_slot(self, hold_id: str, body: dict[str, Any]) -> dict[str, Any]:
        del hold_id, body
        raise CapError("POLICY_VIOLATION", "ledger")


def _in_window(agenda_date: str, start: date | None, until: date | None) -> bool:
    try:
        day = parse_time(agenda_date).date()
    except CapError:
        return False
    return (start is None or day >= start) and (until is None or day < until)
