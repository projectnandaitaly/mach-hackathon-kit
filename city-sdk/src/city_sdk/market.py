"""Day-1 types and verbs. Team code imports this module only."""

from __future__ import annotations

import copy
import logging
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from types import MappingProxyType
from typing import Any, Protocol, TypeGuard, runtime_checkable

from cap.core.errors import CapError
from cap.core.timeutil import format_time, parse_time

from city_sdk.llm import LlmConnector

MARKET_VERBS: tuple[str, ...] = (
    "find_facilities",
    "find_peers",
    "ask",
    "send",
    "rank",
    "remember",
    "recall",
    "history",
    "policy",
    "escalate",
    "rate",
    "hold",
    "commit",
    "release",
    "offer",
    "present_offers",
    "prescribe",
    "verify_prescription",
    "ask_consent",
    "request_data",
    "record_checkin",
    "check_in",
    "file_report",
    "world",
    "agenda_now",
    "active_rules",
    "request_offers",
    "confirm",
    "send_receipt",
    "decline",
    "say",
    "llm",
)
HOLD_TTL_SECONDS = 120
_LOG = logging.getLogger("city_sdk.market")


class OutOfScope(Exception):
    """Profile refusal. The harness signs it. Team code does not."""


_CLINICAL = re.compile(r"diagnos|dosage|posolog|\d+\s*mg", re.IGNORECASE)


def _plain(value: object) -> TypeGuard[str | int | float | bool]:
    return isinstance(value, (str, int, float, bool))


def clinical(text: str) -> bool:
    """Diagnosis or dosage requests a ``no_clinical_advice`` agent must refuse."""

    return _CLINICAL.search(text) is not None


def agenda_time(text: str) -> datetime:
    """Parse an agenda timestamp from a world slot or a message body."""

    return parse_time(text)


@dataclass(frozen=True)
class Message:
    """The inbound envelope as the brain sees it. The body is read-only.

    Structured market fields live in ``body``. Optional free speech is ``body["text"]``,
    also exposed as ``message.text`` (ADR-077). Speech does not change citizen choice or
    the world score; the judge still leak-scans it for over-disclosure.
    """

    type: str
    sender: str
    msg_id: str
    need_id: str | None = None
    conversation_id: str | None = None
    body: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "body", MappingProxyType(copy.deepcopy(dict(self.body))))

    @property
    def text(self) -> str:
        """Free speech on this turn, or an empty string when the sender left none."""

        value = self.body.get("text")
        return value if isinstance(value, str) else ""


_ACTS = frozenset(
    {
        "need.request",
        "offer",
        "confirm",
        "cancel",
        "prescription",
        "consent",
        "data.request",
        "data.response",
        "checkin",
        "report",
        "booking.receipt",
        "escalate.emergency",
        "hold",
        "case.closed",
    }
)


def understood(message: Message) -> Message:
    """Read a chat turn back into the act the reference agents already handle.

    The wire is text. ``key=value`` tokens and a named act are what the citizen's
    template puts in the sentence. A structured message is returned unchanged.
    """

    if message.type != "chat":
        return message
    fields: dict[str, Any] = {}
    kind = "need.request"
    for token in message.text.replace(",", " ").split():
        if token in _ACTS:
            kind = token
            continue
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        if value in ("True", "true"):
            fields[key] = True
        elif value in ("False", "false"):
            fields[key] = False
        else:
            fields[key] = value
    fields.setdefault("text", message.text)
    return Message(
        type=kind,
        sender=message.sender,
        msg_id=message.msg_id,
        need_id=message.need_id,
        conversation_id=message.conversation_id,
        body=fields,
    )


@dataclass(frozen=True)
class Need:
    service: str = ""
    district: int | None = None
    is_emergency: bool = False
    summary: str = ""
    message: Message | None = field(default=None, compare=False)


# The five roles the Index answers for (ADR-085). ``b2b`` is not one of them.
ROLES: tuple[str, ...] = (
    "booking_centre",
    "gp",
    "hospital",
    "specialist_care",
    "family_counseling",
)
FACILITY_ROLES: tuple[str, ...] = ("hospital", "specialist_care", "family_counseling")


def role_for(service: str) -> str | None:
    """The role that offers ``service``: its prefix when that is a role, ``hospital`` for
    any other facility service type, and None for ``b2b``."""

    head = service.split(".", 1)[0]
    if head in ROLES:
        return head
    if head == "b2b" or not head:
        return None
    return "hospital"


@dataclass(frozen=True)
class Peer:
    agent_id: str
    service: str = ""
    district: int | None = None
    role: str = ""
    reputation: int | float = 0
    service_types: tuple[str, ...] = ()
    no_clinical_advice: bool = False
    profiles: tuple[str, ...] = ()


class IndexPort(Protocol):
    async def find(self, *, role: str) -> list[Peer]: ...


class RelayPort(Protocol):
    async def send(
        self,
        agent_id: str,
        body: dict[str, object],
        *,
        traceparent: str | None,
    ) -> None: ...


class MessagePort(Protocol):
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


class CarePort(Protocol):
    """Ledger care registers and the wave clock (ledger.yaml)."""

    async def world(
        self, from_date: date | None = None, until_date: date | None = None
    ) -> dict[str, Any]: ...

    async def agenda_now(self) -> dict[str, Any]: ...

    async def rules(self) -> list[dict[str, Any]]: ...

    async def issue_prescription(self, body: dict[str, Any]) -> dict[str, Any]: ...

    async def prescription(self, prescription_number: str) -> dict[str, Any]: ...

    async def checkin(self, body: dict[str, Any]) -> dict[str, Any]: ...

    async def report(self, body: dict[str, Any]) -> dict[str, Any]: ...

    async def hold_slot(self, facility_id: str, body: dict[str, Any]) -> dict[str, Any]: ...

    async def commit_slot(self, hold_id: str, body: dict[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class NotesPort(Protocol):
    """The seat's durable memory and a journey's hand-overs (ADR-083, ledger.yaml)."""

    async def put_note(self, key: str, value: str, *, need_id: str | None) -> None: ...

    async def get_note(self, key: str) -> str | None: ...

    async def history(self, need_id: str) -> dict[str, Any]: ...


class LedgerPort(Protocol):
    async def hold(self, agent_id: str) -> str: ...

    async def commit(self, hold_id: str) -> None: ...

    async def release(self, hold_id: str) -> None: ...


class AttestPort(Protocol):
    async def rate(self, agent_id: str, score: int) -> None: ...


class EvidencePort(Protocol):
    async def escalate(self, reason: str) -> None: ...

    async def sent(
        self,
        peer: str,
        body: dict[str, object],
        *,
        traceparent: str | None,
    ) -> None: ...


class RankFn(Protocol):
    def __call__(self, peers: list[Peer], *, service: str) -> list[Peer]: ...


class Market:
    """Domain verbs. Fence, lease, and signing stay in the harness."""

    def __init__(
        self,
        *,
        index: IndexPort,
        relay: RelayPort,
        ledger: LedgerPort,
        attest: AttestPort,
        rank: RankFn,
        memory: dict[str, str],
        epoch: Callable[[], int],
        traceparent: str | None,
        escalations: list[str] | None = None,
        evidence: EvidencePort | None = None,
        agent_id: str = "",
        care: CarePort | None = None,
        messages: MessagePort | None = None,
        inbound: Message | None = None,
        llm: LlmConnector | None = None,
        notes: NotesPort | None = None,
    ) -> None:
        self._index = index
        self._notes = notes
        self._relay = relay
        self._ledger = ledger
        self._attest = attest
        self._rank = rank
        self._memory = memory
        self._epoch = epoch
        self._traceparent = traceparent
        self._escalations = escalations
        self._evidence = evidence
        self._agent_id = agent_id
        self._care = care
        self._messages = messages
        self.inbound = inbound
        self._llm = llm
        self.escalated: str | None = None

    @property
    def agent_id(self) -> str:
        """The calling agent. A facility uses it to find its own slots in the world."""

        return self._agent_id

    async def find_facilities(
        self,
        *,
        service: str = "",
        district: int | None = None,
    ) -> list[Peer]:
        """Facilities for ``service``; every facility role when ``service`` is empty."""

        if service:
            return await self.find_peers(service=service, district=district)
        return await self._find_roles(FACILITY_ROLES, service="")

    async def find_peers(
        self, *, role: str = "", service: str = "", district: int | None = None
    ) -> list[Peer]:
        """The latest agent of each team in ``role``, with its reputation, in the Index's
        order. ``service`` picks the role it belongs to; ``district`` is not a filter
        (ADR-085). With neither, every role."""

        del district
        if role:
            if role not in ROLES:
                raise CapError("SCHEMA_INVALID", "role")
            return await self._find_roles((role,), service=service)
        if service:
            found = role_for(service)
            return [] if found is None else await self._find_roles((found,), service=service)
        return await self._find_roles(ROLES, service="")

    async def _find_roles(self, roles: Sequence[str], *, service: str) -> list[Peer]:
        peers: list[Peer] = []
        for role in roles:
            for peer in await self._index.find(role=role):
                peers.append(
                    peer if peer.service or not service else replace(peer, service=service)
                )
        return peers

    async def ask(self, peer: Peer, need: Need) -> None:
        parts = [need.summary]
        if need.service:
            parts.append(f"service={need.service}")
        if need.district is not None:
            parts.append(f"district={need.district}")
        text = " ".join(part for part in parts if part)[:2000] or "request"
        await self._relay.send(peer.agent_id, {"text": text}, traceparent=self._traceparent)

    async def send(self, peer: Peer, need: Need) -> None:
        await self.ask(peer, need)

    async def rank(self, peers: list[Peer], *, service: str = "") -> list[Peer]:
        return self._rank(list(peers), service=service)

    async def remember(self, key: str, value: str) -> None:
        """Keep ``value`` here and in the seat's memory on the Ledger, tagged with the inbound
        need. The agent that takes the seat over reads it (ADR-083). A refused write keeps the
        local copy."""

        self._memory[key] = value
        if self._notes is None:
            return
        need_id = self._need_id(None)
        try:
            await self._notes.put_note(key, value, need_id=need_id)
        except Exception as exc:
            _LOG.warning(
                "note not kept",
                extra={"agent_id": self._agent_id, "need_id": need_id, "reason": str(exc)},
            )

    async def recall(self, key: str) -> str | None:
        """The local copy, else the seat's memory on the Ledger."""

        value = self._memory.get(key)
        if value is not None or self._notes is None:
            return value
        try:
            value = await self._notes.get_note(key)
        except Exception as exc:
            _LOG.warning("note not read", extra={"agent_id": self._agent_id, "reason": str(exc)})
            return None
        if value is not None:
            self._memory[key] = value
        return value

    async def history(self, need_id: str | None = None) -> dict[str, Any]:
        """Every hand-over of the journey, with the conversation the earlier agent had."""

        need = self._need_id(need_id)
        if self._notes is None or need is None:
            raise CapError("POLICY_VIOLATION", "ledger")
        return await self._notes.history(need)

    async def policy(self) -> dict[str, int]:
        return {"epoch": self._epoch()}

    async def escalate(
        self, *, reason: str, citizen: Peer | str | None = None, text: str | None = None
    ) -> None:
        """Record the escalation. With ``citizen``, also tell them with ``escalate.emergency``."""

        self.escalated = reason
        if self._escalations is not None:
            self._escalations.append(reason)
        if self._evidence is not None:
            await self._evidence.escalate(reason)
        if citizen is not None and self._messages is not None:
            await self._post(
                citizen, "escalate.emergency", _speech({"reason": reason}, text), None, None
            )

    async def close_case(
        self, citizen: Peer | str, *, answer: str | None = None, text: str | None = None
    ) -> str:
        """Tell the citizen this agent has finished, hand-offs and ledger writes included
        (ADR-106). The judge settles the journey from here; later facts are not scored.
        ``answer`` is the ``msg_id`` of the final answer, when there is one."""

        body: dict[str, object] = {} if answer is None else {"answer": answer}
        return await self._post(citizen, "case.closed", _speech(body, text), None, None)

    async def rate(self, peer: Peer, *, score: int) -> None:
        await self._attest.rate(peer.agent_id, score)

    async def hold(
        self,
        peer: Peer | None = None,
        *,
        slot_id: str | None = None,
        need_id: str | None = None,
        citizen: Peer | str | None = None,
        prescription_number: str | None = None,
        consent_ref: str | None = None,
        offer_id: str | None = None,
    ) -> str:
        """Hold capacity. With ``slot_id`` this books a world slot of the calling facility."""

        if slot_id is None:
            if peer is None:
                raise ValueError("hold needs a peer or a slot_id")
            return await self._ledger.hold(peer.agent_id)
        need = need_id if need_id is not None else (self.inbound.need_id if self.inbound else None)
        if not need:
            raise ValueError("a world hold needs a need_id")
        body: dict[str, Any] = {
            "offer_id": offer_id if offer_id is not None else "of-" + slot_id,
            "fence": 1,
            "ttl_seconds": HOLD_TTL_SECONDS,
            "slot_id": slot_id,
            "need_id": need,
        }
        if citizen is not None:
            body["citizen"] = _id(citizen)
        if prescription_number is not None:
            body["prescription_number"] = prescription_number
        if consent_ref is not None:
            body["consent_ref"] = consent_ref
        held = await self._ledger_care().hold_slot(self._agent_id, body)
        return str(held["hold_id"])

    async def commit(
        self,
        hold_id: str,
        *,
        consent_ref: str | None = None,
        prescription_number: str | None = None,
    ) -> dict[str, Any] | None:
        """Commit a hold. With ``consent_ref`` the Ledger result carries the signed receipt."""

        if consent_ref is None:
            await self._ledger.commit(hold_id)
            return None
        body: dict[str, Any] = {"fence": 1, "consent_ref": consent_ref}
        if prescription_number is not None:
            body["prescription_number"] = prescription_number
        return await self._ledger_care().commit_slot(hold_id, body)

    async def release(self, hold_id: str) -> None:
        await self._ledger.release(hold_id)

    async def offer(
        self,
        to: Peer | str,
        *,
        slot: datetime | str,
        priority: str,
        expiry: datetime | str,
        need_id: str | None = None,
        conversation_id: str | None = None,
        text: str | None = None,
        **fields: object,
    ) -> str:
        body: dict[str, object] = _speech(
            {
                **fields,
                "slot": _time(slot),
                "priority": priority,
                "expiry": _time(expiry),
            },
            text,
        )
        return await self._post(to, "offer", body, need_id, conversation_id)

    async def present_offers(
        self,
        citizen: Peer | str,
        offers: Sequence[Mapping[str, object]],
        *,
        need_id: str | None = None,
        conversation_id: str | None = None,
    ) -> list[str]:
        sent: list[str] = []
        for row in offers:
            body = {
                key: _time(value) if isinstance(value, datetime) else value
                for key, value in row.items()
            }
            sent.append(await self._post(citizen, "offer", body, need_id, conversation_id))
        return sent

    async def prescribe(
        self,
        citizen: Peer | str,
        *,
        procedure: str,
        priority: str,
        prescription_number: str | None = None,
        symptom_code: str | None = None,
        follow_up_of: str | None = None,
        need_id: str | None = None,
        conversation_id: str | None = None,
        text: str | None = None,
    ) -> dict[str, Any]:
        patient = _id(citizen)
        body: dict[str, Any] = {
            "prescription_number": prescription_number
            if prescription_number is not None
            else "prescription_number-" + uuid.uuid4().hex[:12].upper(),
            "procedure": procedure,
            "priority": priority,
            "issued_by": self._agent_id,
            "patient": patient,
        }
        if symptom_code is not None:
            body["symptom_code"] = symptom_code
        if follow_up_of is not None:
            body["follow_up_of"] = follow_up_of
        journey = self._need_id(need_id)
        if journey is not None:
            body["need_id"] = journey
        record = await self._ledger_care().issue_prescription(body)
        issued = record.get("prescription")
        message = dict(issued) if isinstance(issued, Mapping) else body
        await self._post(patient, "prescription", _speech(message, text), need_id, conversation_id)
        return record

    async def verify_prescription(self, prescription_number: str) -> dict[str, Any]:
        return await self._ledger_care().prescription(prescription_number)

    async def ask_consent(
        self,
        citizen: Peer | str,
        *,
        purpose: str,
        need_id: str | None = None,
        conversation_id: str | None = None,
        text: str | None = None,
    ) -> str:
        body: dict[str, object] = _speech({"tax_code": False, "purpose": purpose}, text)
        return await self._post(citizen, "data.request", body, need_id, conversation_id)

    async def request_data(
        self,
        citizen: Peer | str,
        *,
        purpose: str,
        fields: Sequence[str],
        tax_code: bool = False,
        need_id: str | None = None,
        conversation_id: str | None = None,
        text: str | None = None,
    ) -> str:
        if not fields:
            raise ValueError("request_data needs at least one field")
        body: dict[str, object] = _speech(
            {
                "tax_code": tax_code,
                "purpose": purpose,
                "fields": list(fields),
            },
            text,
        )
        return await self._post(citizen, "data.request", body, need_id, conversation_id)

    async def record_checkin(
        self,
        *,
        booking_id: str,
        citizen: Peer | str,
        agenda_time: datetime | str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"booking_id": booking_id, "citizen": _id(citizen)}
        if agenda_time is not None:
            body["agenda_time"] = _time(agenda_time)
        return await self._ledger_care().checkin(body)

    async def check_in(
        self,
        to: Peer | str,
        *,
        booking_id: str,
        ledger_ref: str,
        agenda_time: datetime | str | None = None,
        need_id: str | None = None,
        conversation_id: str | None = None,
        text: str | None = None,
    ) -> str:
        body: dict[str, object] = {"booking_id": booking_id, "ledger_ref": ledger_ref}
        if agenda_time is not None:
            body["agenda_time"] = _time(agenda_time)
        return await self._post(to, "checkin", _speech(body, text), need_id, conversation_id)

    async def file_report(
        self,
        citizen: Peer | str,
        *,
        booking_id: str,
        outcome_code: str,
        follow_up_prescription_number: str | None = None,
        consent_ref: str | None = None,
        need_id: str | None = None,
        conversation_id: str | None = None,
        text: str | None = None,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {"booking_id": booking_id, "outcome_code": outcome_code}
        if follow_up_prescription_number is not None:
            request["follow_up_prescription_number"] = follow_up_prescription_number
        if consent_ref is not None:
            request["consent_ref"] = consent_ref
        record = await self._ledger_care().report(request)
        body: dict[str, object] = _speech({**request, "report_id": record["report_id"]}, text)
        await self._post(citizen, "report", body, need_id, conversation_id)
        return record

    async def request_offers(
        self,
        facility: Peer | str,
        *,
        service_type: str,
        need_id: str | None = None,
        conversation_id: str | None = None,
        text: str | None = None,
        **fields: object,
    ) -> str:
        """Ask a facility for offers with a structured ``need.request``."""

        body: dict[str, object] = _speech({**fields, "service_type": service_type}, text)
        return await self._post(facility, "need.request", body, need_id, conversation_id)

    async def confirm(
        self,
        to: Peer | str,
        *,
        offer_id: str,
        consent_ref: str,
        need_id: str | None = None,
        conversation_id: str | None = None,
        text: str | None = None,
        **fields: object,
    ) -> str:
        body: dict[str, object] = _speech(
            {**fields, "offer_id": offer_id, "consent_ref": consent_ref}, text
        )
        return await self._post(to, "confirm", body, need_id, conversation_id)

    async def send_receipt(
        self,
        to: Peer | str,
        receipt: Mapping[str, object],
        *,
        need_id: str | None = None,
        conversation_id: str | None = None,
        text: str | None = None,
        **fields: object,
    ) -> str:
        """Deliver a ``booking.receipt``. ``receipt`` is the Ledger's signed receipt."""

        body: dict[str, object] = _speech({**fields, **receipt}, text)
        return await self._post(to, "booking.receipt", body, need_id, conversation_id)

    async def decline(
        self,
        to: Peer | str,
        *,
        offer_id: str,
        need_id: str | None = None,
        conversation_id: str | None = None,
        text: str | None = None,
        **fields: object,
    ) -> str:
        """Withdraw an offer or a booking with a ``cancel`` message."""

        body: dict[str, object] = _speech({**fields, "offer_id": offer_id}, text)
        return await self._post(to, "cancel", body, need_id, conversation_id)

    async def say(
        self,
        to: Peer | str,
        text: str,
        *,
        need_id: str | None = None,
        conversation_id: str | None = None,
    ) -> str:
        """Say one thing, in free text. This is the only message a team agent sends (ADR-084)."""

        if not isinstance(text, str) or not text or len(text) > 2000:
            raise CapError("SCHEMA_INVALID", "text")
        return await self._post(to, "chat", {"text": text}, need_id, conversation_id)

    async def llm(
        self, messages: list[dict[str, str]], *, need_id: str | None = None
    ) -> dict[str, Any]:
        """One chat completion through the City LLM Gateway, billed to the team.

        ``messages`` is the OpenAI list of ``{"role", "content"}``. The answer is
        ``{"model", "content", "prompt_tokens", "completion_tokens"}``. No retry.
        """

        if not isinstance(messages, list) or not messages:
            raise CapError("SCHEMA_INVALID", "messages")
        for item in messages:
            if not isinstance(item, dict):
                raise CapError("SCHEMA_INVALID", "messages")
            if not isinstance(item.get("role"), str) or not isinstance(item.get("content"), str):
                raise CapError("SCHEMA_INVALID", "messages")
        if self._llm is None:
            raise CapError("KEY_UNKNOWN", "llm gateway key is missing")
        sent: list[object] = [dict(item) for item in messages]
        bound = self._need_id(need_id)
        try:
            out = await self._llm.complete(sent, need_id=bound)
        except Exception as exc:
            _LOG.warning(
                "llm call failed",
                extra={
                    "agent_id": self._agent_id,
                    "need_id": bound,
                    "city_fields": {
                        "model": "city-default",
                        "ok": False,
                        "detail": str(exc)[:240],
                    },
                },
            )
            raise
        model = out.get("model")
        content = out.get("content")
        if isinstance(content, str) and len(content) > 160:
            preview: object = content[:160] + "…"
        else:
            preview = content
        _LOG.info(
            "llm call ok",
            extra={
                "agent_id": self._agent_id,
                "need_id": bound,
                "city_fields": {
                    "model": model if isinstance(model, str) else "city-default",
                    "ok": True,
                    "detail": (
                        f"tokens={out.get('prompt_tokens')}/{out.get('completion_tokens')} "
                        f"content={preview!s}"
                    )[:400],
                },
            },
        )
        return out

    async def world(
        self, from_date: date | None = None, until_date: date | None = None
    ) -> dict[str, Any]:
        """The ASST's world slice. Slots cover today plus 14 agenda days unless a window
        ``[from_date, until_date)`` of at most 31 days is asked for (ADR-082)."""

        return await self._ledger_care().world(from_date, until_date)

    async def agenda_now(self) -> datetime:
        clock = await self._ledger_care().agenda_now()
        return parse_time(clock["agenda_now"])

    async def active_rules(self) -> list[dict[str, Any]]:
        """The latest version of each rule already in force at agenda time."""

        now = await self.agenda_now()
        latest: dict[str, dict[str, Any]] = {}
        for rule in await self._ledger_care().rules():
            if parse_time(rule["effective_agenda"]) > now:
                continue
            seen = latest.get(rule["rule_id"])
            if seen is None or rule["version"] > seen["version"]:
                latest[rule["rule_id"]] = rule
        return [latest[key] for key in sorted(latest)]

    async def _post(
        self,
        to: Peer | str,
        message_type: str,
        body: dict[str, object],
        need_id: str | None,
        conversation_id: str | None,
    ) -> str:
        if self._messages is None:
            raise CapError("POLICY_VIOLATION", "relay")
        if message_type != "chat":
            # The verb still names the act. On the wire it is one text (ADR-084).
            raw_text = body.get("text")
            spoken = raw_text if isinstance(raw_text, str) else ""
            parts: list[str] = []
            for key in sorted(body):
                value = body[key]
                if key != "text" and _plain(value):
                    parts.append(f"{key}={value}")
            extra = " ".join(parts)
            text = " ".join(part for part in (spoken, message_type, extra) if part)[:2000]
            body = {"text": text or message_type}
            message_type = "chat"
        inbound = self.inbound
        return await self._messages.post(
            to=_id(to),
            message_type=message_type,
            body=body,
            need_id=self._need_id(need_id),
            conversation_id=(
                conversation_id
                if conversation_id is not None
                else (inbound.conversation_id if inbound else None)
            ),
            traceparent=self._traceparent,
        )

    def _need_id(self, need_id: str | None) -> str | None:
        if need_id is not None:
            return need_id
        return self.inbound.need_id if self.inbound else None

    def _ledger_care(self) -> CarePort:
        if self._care is None:
            raise CapError("POLICY_VIOLATION", "ledger")
        return self._care


def _id(who: Peer | str) -> str:
    return who.agent_id if isinstance(who, Peer) else who


def _time(value: datetime | str) -> str:
    return format_time(value) if isinstance(value, datetime) else value


def _speech(body: dict[str, object], text: str | None) -> dict[str, object]:
    """Attach optional free speech without touching structured market fields (ADR-077)."""

    if text is not None:
        body["text"] = text
    return body
