"""Characters and moves for ``city run``.

The person at the keyboard plays one role and talks to the local brain.
Moves are the messages that role actually sends in the care journey.
The brain's answer stays structured; this module only explains it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cap.core.errors import CapError
from city_sdk.city import LEDGER

CHARACTERS: tuple[tuple[str, str], ...] = (
    ("citizen", "A citizen. A sentence is a need."),
    ("agent", "Another agent. A sentence is a chat; put the act word in it."),
    ("ledger", "The ledger. A sentence is its answer (held, committed, world, prescription)."),
)

# Meta commands. The whole line, any prompt: before a character is picked and after.
SESSION_COMMANDS: tuple[tuple[str, str], ...] = (
    ("\\changeCharacter", "Back to the character menu."),
    ("\\quitChat", "Leave the chat and return to the shell."),
)

SENDERS: dict[str, str] = {
    "citizen": "citizen:local/dev",
    "agent": "agent:local/peer",
    "ledger": LEDGER,
}

_ALIASES: dict[str, str] = {
    "citizen": "citizen",
    "agent": "agent",
    "ledger": "ledger",
    "city:ledger": "ledger",
}

# Catalogue codes the local world and the city wave share.
CODES: dict[str, str] = {
    "CONS-GIN": "gynecology counseling; no prescription",
    "VIS-FIS": "physiatry visit at the hospital; needs a prescription",
    "VIS-CARD": "cardiology visit; needs a prescription",
    "CTRL-CARD": "cardiology follow-up; needs a prescription",
    "SYM-PALPITATIONS": (
        "protocol row for VIS-CARD, priority B; red flag if the text says chest pain"
    ),
    "SYM-BACK": "protocol row for VIS-FIS, priority B",
    "SYM-KNEE": "protocol row for VIS-FIS, priority D",
}

_SILENCE: dict[str, str] = {
    "gp": (
        "No market action. The GP template prescribes only when the body "
        "carries a protocol symptom_code (try 1, or /need symptom=SYM-PALPITATIONS). "
        "A sentence alone does not set that code."
    ),
    "booking_centre": (
        "No market action. The booking-centre template looks for a "
        "prescription_number, then asks the first facility. A sentence with "
        "no prescription still runs that path only when find_facilities returns someone."
    ),
    "specialist": (
        "No market action. The specialist template offers a slot only when "
        "the body has a prescription_number whose procedure matches a future slot "
        "(try /rx, then /need rx=<number>)."
    ),
    "family_counseling": (
        "No market action. The family-counseling template offers CONS-GIN "
        "on a need.request, and records a check-in on /checkin <booking_id>."
    ),
    "hospital": (
        "No market action. The hospital template holds the first peer on a "
        "need, and records a check-in on /checkin <booking_id>."
    ),
}


@dataclass(frozen=True)
class PlayMove:
    """One message this character can send to the agent under test."""

    command: str
    title: str
    meaning: str
    payload: dict[str, object]


@dataclass(frozen=True)
class ChatTurn:
    """What one typed line asked the REPL to do."""

    kind: str
    actor: str | None = None
    payload: dict[str, object] | None = None
    message: str = ""


def agent_part(card: Mapping[str, Any]) -> str:
    """Role family of the local card: who is under test."""
    role = card.get("role")
    service = ""
    raw = card.get("service_types")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item:
                service = item
                break
    if role == "gp" or service == "gp":
        return "gp"
    if role == "booking_centre" or service == "booking_centre":
        return "booking_centre"
    if service.startswith("family_counseling"):
        return "family_counseling"
    if service.startswith("specialist_care"):
        return "specialist"
    return "hospital"


def resolve_actor(text: str) -> str | None:
    """Map a name or alias to a character id."""
    return _ALIASES.get(text.strip().lower().replace(" ", "_"))


def moves_for(actor: str, target: str, *, service: str) -> list[PlayMove]:
    """Messages ``actor`` sends to ``target`` in the care journey."""
    sender = SENDERS[actor]
    if actor == "citizen" and target == "gp":
        return [
            PlayMove(
                "/need symptom=SYM-PALPITATIONS",
                "Palpitations",
                "Citizen reports SYM-PALPITATIONS. "
                + CODES["SYM-PALPITATIONS"]
                + ". The GP template should prescribe VIS-CARD at priority B.",
                _need(sender, service, "palpitations", {"symptom_code": "SYM-PALPITATIONS"}),
            ),
            PlayMove(
                "/need symptom=SYM-BACK",
                "Back pain",
                "Citizen reports SYM-BACK. "
                + CODES["SYM-BACK"]
                + ". The GP template should prescribe VIS-FIS at priority B.",
                _need(sender, service, "back pain", {"symptom_code": "SYM-BACK"}),
            ),
            PlayMove(
                "/need symptom=SYM-PALPITATIONS",
                "Palpitations with chest pain",
                'Same symptom, but the text contains the red flag "chest pain". '
                "The GP template should escalate and not prescribe.",
                _need(
                    sender,
                    service,
                    "chest pain",
                    {"symptom_code": "SYM-PALPITATIONS", "text": "chest pain"},
                ),
            ),
            _emergency(
                sender,
                service,
                "The GP template escalates a red flag and does not prescribe.",
            ),
        ]
    if actor == "citizen" and target == "booking_centre":
        return [
            PlayMove(
                "/need rx=rx-local-1",
                "Book with a prescription",
                "Citizen asks to book rx-local-1. Run /rx first so that number exists. "
                "The template verifies it and asks the first facility the Index returns. "
                "That order is not a recommendation.",
                _need(sender, service, "book", {"prescription_number": "rx-local-1"}),
            ),
            PlayMove(
                "/confirm offer=of-slot-local-1 consent=local-ok",
                "Accept an offer",
                "Citizen confirms offer of-slot-local-1 with consent local-ok. "
                "The template centro prenotazioni does not read confirm; your brain can.",
                {
                    "type": "confirm",
                    "from": sender,
                    "body": {"offer_id": "of-slot-local-1", "consent_ref": "local-ok"},
                },
            ),
            PlayMove(
                "/data lang=it",
                "Share a field",
                "Citizen answers a data.request and shares lang=it. "
                "The template does not read data.response; your brain can.",
                {
                    "type": "data.response",
                    "from": sender,
                    "body": {"purpose": "booking", "provided": {"lang": "it"}},
                },
            ),
            _emergency(
                sender,
                service,
                "The booking-centre template escalates and does not ask a facility.",
            ),
        ]
    if actor == "citizen" and target == "specialist":
        return [
            PlayMove(
                "/need rx=rx-local-1",
                "Visit with a prescription",
                "Citizen arrives with rx-local-1. Run /rx first. "
                "The template verifies the prescription and offers the first future "
                "VIS-CARD slot. " + CODES["VIS-CARD"] + ".",
                _need(sender, service, "visit", {"prescription_number": "rx-local-1"}),
            ),
            _checkin(sender, "The specialist template records the check-in and files a report."),
            _emergency(
                sender,
                service,
                "The specialist template escalates and does not offer a slot.",
            ),
        ]
    if actor == "citizen" and target == "family_counseling":
        return [
            PlayMove(
                "/need",
                "Ask for counseling",
                "Free access: no prescription. The template should offer CONS-GIN. "
                + CODES["CONS-GIN"]
                + ".",
                _need(sender, service, "I need a visit", {}),
            ),
            PlayMove(
                "/confirm offer=of-slot-local-1",
                "Accept the offer",
                "Citizen confirms of-slot-local-1. The template does not book on confirm; "
                "your brain can.",
                {"type": "confirm", "from": sender, "body": {"offer_id": "of-slot-local-1"}},
            ),
            _checkin(sender, "The template records the check-in and confirms it."),
            _emergency(sender, service, "The template escalates and does not offer a slot."),
        ]
    if actor == "citizen" and target == "hospital":
        return [
            PlayMove(
                "/need",
                "Ask the hospital",
                "A need that is not a check-in. The hospital template holds the first "
                "peer from the Index. It does not offer its own VIS-FIS slot "
                "(the specialist template is the one that offers).",
                _need(sender, service, "visit", {}),
            ),
            _checkin(sender, "The hospital template records the check-in and confirms it."),
            _emergency(sender, service, "The template escalates and does not hold a peer."),
        ]
    if actor == "agent":
        procedure = _procedure(target)
        return [
            PlayMove(
                "confirm offer_id=of-slot-local-1 consent_ref=local-ok slot_id=slot-local-1",
                "Confirm a slot",
                "An agent confirms of-slot-local-1. The brain reads confirm and the tokens.",
                _chat(
                    sender,
                    "confirm offer_id=of-slot-local-1 consent_ref=local-ok "
                    "slot_id=slot-local-1 prescription_number=rx-local-1",
                ),
            ),
            PlayMove(
                f"offer procedure={procedure}",
                "Offer a slot",
                f"An agent offers {procedure}. " + CODES.get(procedure, ""),
                _chat(
                    sender,
                    f"offer offer_id=of-slot-local-1 slot_id=slot-local-1 "
                    f"procedure={procedure} priority=B",
                ),
            ),
            PlayMove(
                "cancel offer_id=of-slot-local-1 reason=slot_gone",
                "Cancel an offer",
                "An agent withdraws of-slot-local-1.",
                _chat(sender, "cancel offer_id=of-slot-local-1 reason=slot_gone"),
            ),
            PlayMove(
                "booking.receipt booking_id=bk-1 ledger_ref=c-1",
                "Send a receipt",
                "An agent sends the booking receipt.",
                _chat(sender, "booking.receipt booking_id=bk-1 ledger_ref=c-1"),
            ),
        ]
    if actor == "ledger":
        procedure = _procedure(target)
        return [
            PlayMove(
                "held hold_id=bk-1 slot_id=slot-local-1 fence=1",
                "Hold accepted",
                "The ledger held slot-local-1.",
                _chat(sender, "held hold_id=bk-1 slot_id=slot-local-1 fence=1"),
            ),
            PlayMove(
                "committed booking_id=bk-1 ledger_ref=c-1 receipt=local",
                "Booking committed",
                "The ledger committed the hold.",
                _chat(sender, "committed booking_id=bk-1 ledger_ref=c-1 receipt=local"),
            ),
            PlayMove(
                "prescription prescription_number=rx-local-1",
                "Prescription on file",
                f"The ledger returns rx-local-1 for {procedure}.",
                _chat(
                    sender,
                    f"prescription prescription_number=rx-local-1 procedure={procedure} "
                    "priority=B state=issued",
                ),
            ),
            PlayMove(
                "world slots=1",
                "World slice",
                "One local slot, in the shape a world answer uses.",
                _chat(
                    sender,
                    "world slots=1 slot_id=slot-local-1 "
                    f"procedure={procedure} doctor_id=doc-local-1",
                ),
            ),
        ]
    return []


def interpret(
    line: str,
    *,
    actor: str | None,
    service: str,
    moves: list[PlayMove],
) -> ChatTurn:
    """Map one chat line to a REPL action. Slash commands are the whole line."""
    text = line.strip()
    if not text:
        return ChatTurn("noop")
    session = _session_turn(text)
    if session is not None:
        return session
    if text in {"/moves", "/who"}:
        return ChatTurn("moves")
    if text == "/rx":
        return ChatTurn("rx")
    if text == "/as" or text.startswith("/as "):
        name = text[3:].strip()
        if not name:
            return ChatTurn("actor", message="pick")
        picked = resolve_actor(name)
        if picked is None:
            return ChatTurn("error", message=f"Unknown character: {name}")
        return ChatTurn("actor", actor=picked)
    if actor is None:
        if text.isdigit():
            index = int(text)
            if 1 <= index <= len(CHARACTERS):
                return ChatTurn("actor", actor=CHARACTERS[index - 1][0])
            return ChatTurn("error", message=f"No character {index}")
        picked = resolve_actor(text)
        if picked is not None:
            return ChatTurn("actor", actor=picked)
        return ChatTurn("error", message="Pick who you are first: 1–3, a name, or /as <name>.")
    if text.isdigit():
        index = int(text)
        if 1 <= index <= len(moves):
            return ChatTurn("send", payload=dict(moves[index - 1].payload))
        return ChatTurn("error", message=f"No move {index} — type /moves")
    sender = SENDERS[actor]
    if actor == "ledger" or (actor == "agent" and not text.startswith("/")):
        return ChatTurn("send", payload=_chat(sender, text))
    from city_deploy.local import chat_line_to_payload

    try:
        payload = chat_line_to_payload(text, service=service, sender=sender)
    except CapError as exc:
        return ChatTurn("error", message=f"{exc.code}  {exc.message}")
    if payload is None:
        return ChatTurn("noop")
    return ChatTurn("send", payload=payload)


def explain_turn(
    actions: list[object],
    posts: list[dict[str, object]],
    *,
    target: str,
) -> list[str]:
    """One plain sentence per structured action. Empty when the brain did nothing."""
    if not actions:
        return [_SILENCE.get(target, "No market action.")]
    lines: list[str] = []
    post_at = 0
    for action in actions:
        if not isinstance(action, dict):
            continue
        if action.get("kind") == "post" and post_at < len(posts):
            body = posts[post_at].get("body")
            post_at += 1
            lines.append(_explain_post(action, body if isinstance(body, dict) else {}))
        else:
            lines.append(_explain_action(action))
    return lines


def _session_turn(text: str) -> ChatTurn | None:
    """Quit or return to the character menu. ``None`` when the line is not one of those."""
    folded = text.casefold()
    if folded in {"/quit", "/exit", "/q", "\\quitchat"}:
        return ChatTurn("quit")
    if folded == "\\changecharacter":
        return ChatTurn("actor", message="pick")
    return None


def line_hint(actor: str) -> str:
    """What a free-text line becomes for this sender."""
    if actor == "agent":
        return (
            "A sentence is a chat from an agent. "
            "Put the act word and its key=value tokens on that line."
        )
    if actor == "ledger":
        return (
            "A sentence is a ledger answer. "
            "The brain reads the first word, such as held, committed, world, or prescription."
        )
    return (
        "A sentence is a need.request. It carries your words only, "
        "not a symptom code or a prescription."
    )


def _procedure(target: str) -> str:
    return {
        "hospital": "VIS-FIS",
        "specialist": "VIS-CARD",
        "family_counseling": "CONS-GIN",
    }.get(target, "VIS-CARD")


def _chat(sender: str, text: str) -> dict[str, object]:
    return {"type": "chat", "from": sender, "body": {"text": text}}


def _need(
    sender: str,
    service: str,
    summary: str,
    extra: dict[str, object],
) -> dict[str, object]:
    body: dict[str, object] = {"service": service, "summary": summary, "text": summary, **extra}
    return {
        "type": "need.request",
        "from": sender,
        "service": service,
        "summary": summary,
        "body": body,
    }


def _emergency(sender: str, service: str, meaning: str) -> PlayMove:
    return PlayMove(
        "/emergency",
        "Emergency",
        meaning,
        {
            "type": "need.request",
            "from": sender,
            "service": service,
            "summary": "emergency",
            "is_emergency": True,
            "body": {
                "service": service,
                "summary": "emergency",
                "text": "emergency",
                "is_emergency": True,
            },
        },
    )


def _checkin(sender: str, meaning: str) -> PlayMove:
    return PlayMove(
        "/checkin bk-local-1",
        "Check in",
        meaning + " booking_id bk-local-1 is a local stand-in; the ledger accepts it in this chat.",
        {"type": "checkin", "from": sender, "body": {"booking_id": "bk-local-1"}},
    )


def _explain_action(action: Mapping[str, object]) -> str:
    kind = str(action.get("kind", ""))
    if kind == "escalate":
        reason = action.get("reason", "red_flag")
        return (
            f"Escalation ({reason}). The agent stopped the normal step. "
            "It did not prescribe or offer on this turn."
        )
    if kind == "hold":
        peer = action.get("peer", "")
        return (
            f"Hold on {peer}. The template kept the first peer the Index returned. "
            "That order is not a recommendation."
        )
    if kind == "send":
        peer = action.get("peer", "")
        return f"Ask {peer}. The agent forwarded the need. The Index order is not a recommendation."
    if kind in {"prescribe", "prescription"}:
        detail = action.get("detail", "")
        return (
            f"Prescription register ({detail}). The GP wrote a protocol prescription. "
            "The citizen takes that number to the centro prenotazioni."
        )
    if kind == "checkin":
        return f"Check-in on the local ledger ({action.get('detail', '')})."
    if kind == "report":
        return "Report filed for this booking."
    if kind == "say":
        return _said(action)
    if kind == "ledger":
        text = action.get("text", "")
        spoken = text.strip() if isinstance(text, str) else ""
        return f"Ledger: {spoken or '(no text)'}"
    return f"{kind}: a market action."


def _said(action: Mapping[str, object]) -> str:
    """The words the brain sent, and who they were for."""
    peer = action.get("to", "")
    who = peer.strip() if isinstance(peer, str) and peer.strip() else "the room"
    text = action.get("text", "")
    spoken = text.strip() if isinstance(text, str) else ""
    status = action.get("status", "")
    mark = f" ({status})" if isinstance(status, str) and status else ""
    return f"To {who}{mark}: {spoken or '(no text)'}"


def _explain_post(action: Mapping[str, object], body: Mapping[str, object]) -> str:
    message_type = str(action.get("type", ""))
    procedure = body.get("procedure")
    code = procedure if isinstance(procedure, str) else ""
    gloss = CODES.get(code, "")
    named = f"{code} — {gloss}" if code and gloss else code
    if message_type == "offer":
        priority = body.get("priority", "")
        return (
            f"Offer {named or '(no procedure)'}, priority {priority}. "
            "A citizen chooses from these fields, not from free text."
        )
    if message_type == "prescription":
        priority = body.get("priority", "")
        symptom = body.get("symptom_code", "")
        return (
            f"Prescription {named or code}, priority {priority}, symptom {symptom}. "
            "This is the protocol row, not a diagnosis."
        )
    if message_type in {"checkin", "booking.receipt"}:
        return f"Message {message_type}. The facility is answering with the register's values."
    if message_type == "cancel":
        return f"Cancel ({body.get('reason', '')}). The agent withdrew the offer or the booking."
    return f"Message {message_type}."
