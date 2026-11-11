"""Day-1 types and the three verbs. Team code imports this module only (ADR-110).

A brain finds agents, says something to an agent, a citizen, or the Ledger, and reads its own
mailbox. Ledger work is a message to ``LEDGER``; its answer arrives as a later inbound message.
"""

from __future__ import annotations

import copy
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Protocol

from cap.core.errors import CapError
from cap.core.timeutil import parse_time

from city_sdk.llm import LlmConnector

CITY_VERBS: tuple[str, ...] = ("find", "say", "history", "llm")
LEDGER = "city:ledger"
STATUSES: tuple[str, ...] = ("wip", "closed")
MAX_TEXT = 2000
MAX_QUERY = 500
_LOG = logging.getLogger("city_sdk.city")


class OutOfScope(Exception):
    """Profile refusal. The harness signs it. Team code does not."""


_CLINICAL = re.compile(r"diagnos|\bdos(?:e|es|age|ing)\b|posolog|\d+\s*mg", re.IGNORECASE)


def clinical(text: str) -> bool:
    """Diagnosis or dosage requests a ``no_clinical_advice`` agent must refuse."""

    return _CLINICAL.search(text) is not None


def agenda_time(text: str) -> datetime:
    """Parse an agenda timestamp from a Ledger answer or a message."""

    return parse_time(text)


def is_citizen(party: str) -> bool:
    return party.startswith("citizen:")


def _agent_label(agent_id: str) -> str:
    """The name after ``agent:<team>/``, used as the speaker of a model reply."""

    name = agent_id.rsplit("/", 1)[-1].strip()
    return name or "agent"


def _llm_speaker(role: str, agent: str) -> str:
    """A prompt line speaks as the citizen, unless it is already the agent's own line."""

    if role == "assistant":
        return agent
    if role == "user":
        return "citizen"
    return role or "citizen"


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _log_llm(kind: str, label: str, text: str, *, agent_id: str, need_id: str | None) -> None:
    """One spoken line. ``kind`` is ``in`` for the prompt and ``out`` for the reply."""

    _LOG.info(
        "%s > %s",
        label,
        _one_line(text),
        extra={"agent_id": agent_id, "need_id": need_id, "llm_turn": kind},
    )


@dataclass(frozen=True)
class Message:
    """One mailbox message as the brain sees it. The body is read-only.

    Every message between parties is free text, ``message.text`` (ADR-084). A message to a
    citizen also carries ``status`` (``wip`` or ``closed``, ADR-110).
    """

    type: str
    sender: str
    msg_id: str
    need_id: str | None = None
    conversation_id: str | None = None
    body: Mapping[str, Any] = field(default_factory=dict)
    recipient: str = ""
    sent_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "body", MappingProxyType(copy.deepcopy(dict(self.body))))

    @property
    def text(self) -> str:
        """The free text of this message, or an empty string when there is none."""

        value = self.body.get("text")
        return value if isinstance(value, str) else ""

    @property
    def status(self) -> str | None:
        value = self.body.get("status")
        return value if value in STATUSES else None


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
    """Read a chat turn back into the act its ``key=value`` tokens name.

    The wire is text. A named act and ``key=value`` tokens are what citizens, the judge, and
    the Ledger read. A structured message is returned unchanged.
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
        recipient=message.recipient,
        sent_at=message.sent_at,
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
    async def search(self, query: str) -> list[Peer]: ...


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


class HistoryPort(Protocol):
    async def history(self, *, peer: str | None, need_id: str | None) -> list[Message]: ...


class City:
    """Find, say, history. Fence, lease, signing, and transport stay in the harness."""

    def __init__(
        self,
        *,
        index: IndexPort,
        messages: MessagePort,
        history: HistoryPort | None = None,
        agent_id: str = "",
        inbound: Message | None = None,
        llm: LlmConnector | None = None,
        traceparent: str | None = None,
        address: str = "",
    ) -> None:
        self._index = index
        self._messages = messages
        self._history = history
        self._agent_id = agent_id
        self.inbound = inbound
        self._llm = llm
        self._traceparent = traceparent
        self._address = address.strip() if isinstance(address, str) else ""

    @property
    def agent_id(self) -> str:
        """The calling agent."""

        return self._agent_id

    @property
    def address(self) -> str:
        """This agent's own civic street. Empty when the city has not assigned one."""

        return self._address

    async def find(self, query: str) -> list[Peer]:
        """Agents for a natural-language ``query``. The Index reads the families the text
        names and returns their agents unordered; choosing one is the brain's job."""

        if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY:
            raise CapError("SCHEMA_INVALID", "query")
        return await self._index.search(query)

    async def say(self, to: Peer | str, text: str, *, status: str | None = None) -> str:
        """Send one free-text message to an agent, a citizen, or ``LEDGER``.

        ``status`` goes only to a citizen: ``wip`` (the default) or ``closed``. ``closed``
        ends the case and the judge settles the journey from that message (ADR-106).
        """

        recipient = to.agent_id if isinstance(to, Peer) else to
        if not isinstance(recipient, str) or not recipient:
            raise CapError("SCHEMA_INVALID", "to")
        if not isinstance(text, str) or not text or len(text) > MAX_TEXT:
            raise CapError("SCHEMA_INVALID", "text")
        body: dict[str, object] = {"text": text}
        if is_citizen(recipient):
            chosen = "wip" if status is None else status
            if chosen not in STATUSES:
                raise CapError("SCHEMA_INVALID", "status is wip or closed")
            body["status"] = chosen
        elif status is not None:
            raise CapError("SCHEMA_INVALID", "status goes only to a citizen")
        inbound = self.inbound
        return await self._messages.post(
            to=recipient,
            message_type="chat",
            body=body,
            need_id=inbound.need_id if inbound else None,
            conversation_id=inbound.conversation_id if inbound else None,
            traceparent=self._traceparent,
        )

    async def history(
        self, peer: Peer | str | None = None, *, need_id: str | None = None
    ) -> list[Message]:
        """This agent's own messages, sent and received, oldest first. ``peer`` keeps the
        ones exchanged with that party, ``need_id`` the ones of that case."""

        party = peer.agent_id if isinstance(peer, Peer) else peer
        rows: list[Message] = []
        if self._history is not None:
            rows = await self._history.history(peer=party, need_id=need_id)
        inbound = self.inbound
        if (
            inbound is not None
            and inbound.msg_id
            and all(row.msg_id != inbound.msg_id for row in rows)
            and (party is None or party in (inbound.sender, inbound.recipient))
            and (need_id is None or inbound.need_id == need_id)
        ):
            rows.append(inbound)
        return rows

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
        bound = need_id if need_id is not None else (self.inbound.need_id if self.inbound else None)
        agent = _agent_label(self._agent_id)
        for item in messages:
            _log_llm(
                "in",
                _llm_speaker(item["role"], agent),
                item["content"],
                agent_id=self._agent_id,
                need_id=bound,
            )
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
        _log_llm(
            "out",
            agent,
            content if isinstance(content, str) else "",
            agent_id=self._agent_id,
            need_id=bound,
        )
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
