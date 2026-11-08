"""Family counseling starter (role facility, ``family_counseling.<service>``). Free access.

Works as-is for the base journeys: offers own free-access slots without a prescription, books,
records the check-in, then closes the case (``case.closed``) so the judge starts at once.
"""

import hashlib
import json
from datetime import timedelta

from city_sdk.market import Market, Message, Need, OutOfScope, agenda_time, clinical, understood

FAMILY = "family_counseling"
OUTCOME = "VISITA-OK"
MAX_OFFERS = 5
DEADLINE_DAYS = {"B": 2, "D": 5}


async def main(need: Need, market: Market) -> None:
    if need.summary == "__error__":
        raise RuntimeError("application")
    if clinical(need.summary):
        raise OutOfScope()
    message = understood(need.message) if need.message is not None else None
    if need.is_emergency or (message is not None and message.body.get("is_emergency") is True):
        await market.escalate(reason="red_flag", text="I am escalating this now.")
        return
    handler = HANDLERS.get(message.type) if message is not None else None
    if message is not None and handler is not None:
        await handler(market, message)


# --- offers ------------------------------------------------------------------------------


async def on_need(market: Market, message: Message) -> None:
    body = message.body
    from_citizen = message.sender.startswith("citizen:")
    if not from_citizen and not isinstance(body.get("procedure"), str):
        return
    world = await read_world(market)
    if red_flag(str(body.get("text") or ""), world):
        await market.escalate(reason="red_flag", text="This needs urgent care.")
        return
    now = await market.agenda_now()
    rules = {str(rule["rule_id"]) for rule in await market.active_rules()}
    if not from_citizen:
        await offer_to_booking_centre(market, message, world, now, rules)
        return
    if FAMILY == "family_counseling":
        free = {
            row["code"] for row in world.get("catalogue", []) if not row["requires_prescription"]
        }
        slots = [slot for slot in own_slots(market, world, now) if slot["procedure"] in free]
        await offer_slots(market, message, world, slots[:3], "P", None)
        return
    number = body.get("prescription_number")
    if not isinstance(number, str):
        return
    try:
        issued = (await market.verify_prescription(number))["prescription"]
    except Exception:
        return
    priority = str(issued["priority"])
    slots = window(own_slots(market, world, now, issued["procedure"]), priority, now, rules)
    await offer_slots(market, message, world, slots[:3], priority, number)


async def offer_to_booking_centre(market, message, world, now, rules) -> None:
    body = message.body
    request_id = str(body.get("request_id") or message.msg_id)
    priority = str(body.get("priority") or "D")
    slots = own_slots(market, world, now, str(body["procedure"]))
    slots = criteria(window(slots, priority, now, rules), world, body)[:MAX_OFFERS]
    if not slots:
        await market.decline(message.sender, offer_id=request_id, reason="no_slot")
        return
    for index, slot in enumerate(slots):
        extra = {"request_id": request_id, "last": index == len(slots) - 1}
        await offer_slots(market, message, world, [slot], priority, None, extra)


async def offer_slots(market, message, world, slots, priority, number, extra=None) -> None:
    state = await load(market, message)
    for slot in slots:
        doctor = doctor_of(world, str(slot["doctor_id"]))
        offer_id = "of-" + str(slot["slot_id"])
        fields = {
            "offer_id": offer_id,
            "slot_id": slot["slot_id"],
            "doctor_id": slot["doctor_id"],
            "facility": slot["facility"],
            "procedure": slot["procedure"],
            "agenda_date": slot["agenda_date"],
            "agenda_day": slot["agenda_day"],
            "doctor": {"languages": list(doctor.get("languages", [])), "gender": doctor.get("gender")},
            **(extra or {}),
        }
        if number is not None:
            fields["prescription_number"] = number
        state.setdefault("offers", {})[offer_id] = {
            "slot_id": slot["slot_id"],
            "prescription_number": number,
        }
        await market.offer(
            message.sender, slot=slot["agenda_date"], priority=priority, expiry=slot["agenda_date"], **fields
        )
    await save(market, message, state)


# --- booking -----------------------------------------------------------------------------


async def on_confirm(market: Market, message: Message) -> None:
    body = message.body
    offer_id = str(body.get("offer_id") or "")
    consent = body.get("consent_ref")
    if not offer_id or not isinstance(consent, str):
        return
    state = await load(market, message)
    known = state.get("offers", {}).get(offer_id, {})
    slot_id = body.get("slot_id", known.get("slot_id"))
    if not isinstance(slot_id, str):
        await market.decline(message.sender, offer_id=offer_id, reason="unknown_offer")
        return
    number = body.get("prescription_number", known.get("prescription_number"))
    citizen = message.sender if message.sender.startswith("citizen:") else body.get("citizen")
    done = state.get("receipts", {}).get(offer_id)
    if done is not None:
        await market.send_receipt(message.sender, done)
        return
    cross = body.get("cross_asst_consent")
    number = number if isinstance(number, str) else None
    try:
        hold_id = await market.hold(
            slot_id=slot_id,
            need_id=message.need_id,
            citizen=citizen if isinstance(citizen, str) else None,
            prescription_number=number,
            consent_ref=cross if isinstance(cross, str) else None,
            offer_id=offer_id,
        )
        result = await market.commit(hold_id, consent_ref=consent, prescription_number=number)
    except Exception as exc:
        # A 4xx is final: decline, never retry the same hold.
        await market.decline(message.sender, offer_id=offer_id, reason=str(getattr(exc, "code", "ERROR")))
        return
    receipt = dict((result or {}).get("receipt", {}))
    state.setdefault("receipts", {})[offer_id] = receipt
    await save(market, message, state)
    slot = slot_by_id(await read_world(market), slot_id)
    await market.remember(
        "booking:" + str(receipt.get("booking_id")),
        json.dumps({"procedure": slot.get("procedure"), "need_id": message.need_id}),
    )
    await market.send_receipt(message.sender, receipt, offer_id=offer_id, facility=market.agent_id)


# --- check-in and report -----------------------------------------------------------------


async def on_checkin(market: Market, message: Message) -> None:
    booking_id = str(message.body.get("booking_id") or "")
    if not booking_id or not message.sender.startswith("citizen:"):
        return
    try:
        recorded = await market.record_checkin(booking_id=booking_id, citizen=message.sender)
    except Exception:
        return
    await market.check_in(
        message.sender,
        booking_id=booking_id,
        ledger_ref=recorded["ledger_ref"],
        agenda_time=recorded["agenda_time"],
        text="Check-in recorded. Welcome.",
    )
    if FAMILY != "specialist_care":
        await market.close_case(message.sender, text="Your visit is registered. All done.")
        return
    consent = message.body.get("consent_ref")
    if isinstance(consent, str):
        await file(market, message, booking_id, consent)
        return
    state = await load(market, message)
    state["report_for"] = booking_id
    await save(market, message, state)
    await market.ask_consent(message.sender, purpose="report_sharing")


async def on_consent(market: Market, message: Message) -> None:
    state = await load(market, message)
    booking_id = state.pop("report_for", None)
    consent = message.body.get("consent_ref")
    if not isinstance(booking_id, str) or not isinstance(consent, str):
        return
    await save(market, message, state)
    await file(market, message, booking_id, consent)


async def on_data(market: Market, message: Message) -> None:
    """A refused consent still gets the report filed, not shared."""

    state = await load(market, message)
    booking_id = state.pop("report_for", None)
    if not isinstance(booking_id, str):
        return
    await save(market, message, state)
    await file(market, message, booking_id, None)


async def file(market: Market, message: Message, booking_id: str, consent) -> None:
    raw = await market.recall("booking:" + booking_id)
    booked = json.loads(raw) if raw else {}
    world = await read_world(market)
    follow_up = None
    for row in world.get("protocols", []):
        step = row.get("follow_up")
        if row.get("procedure") == booked.get("procedure") and isinstance(step, dict):
            issued = await market.prescribe(
                message.sender,
                procedure=str(step["procedure"]),
                priority=str(step["priority"]),
                follow_up_of=booking_id,
                prescription_number=number_for(market, message, "F"),
            )
            follow_up = str(issued["prescription_number"])
            break
    await market.file_report(
        message.sender,
        booking_id=booking_id,
        outcome_code=OUTCOME,
        follow_up_prescription_number=follow_up,
        consent_ref=consent,
        text="Visit complete; report filed.",
    )
    # With a follow-up the citizen goes back to a booking centre; the city then ignores this.
    await market.close_case(message.sender, text="My part is done.")


HANDLERS = {
    "need.request": on_need,
    "confirm": on_confirm,
    "checkin": on_checkin,
    "consent": on_consent,
    "data.response": on_data,
}


# --- world helpers -----------------------------------------------------------------------


async def read_world(market: Market):
    world = (await market.world()).get("world", {})
    return world if isinstance(world, dict) else {}


def red_flag(text: str, world) -> bool:
    lowered = text.lower()
    return bool(lowered) and any(
        str(flag).lower() in lowered
        for row in world.get("protocols", [])
        for flag in row.get("red_flags", [])
    )


def own_slots(market: Market, world, now, procedure=None):
    rows = [
        dict(slot)
        for slot in world.get("slots", [])
        if slot["facility"] == market.agent_id
        and (procedure is None or slot["procedure"] == procedure)
        and agenda_time(slot["agenda_date"]) >= now
    ]
    return sorted(rows, key=lambda slot: (agenda_time(slot["agenda_date"]), slot["slot_id"]))


def criteria(slots, world, body):
    """A booking centre may narrow a cross-ASST search by doctor language and day; nothing else."""

    languages = body.get("languages")
    days = body.get("days")
    if isinstance(languages, list) and languages:
        slots = [
            slot
            for slot in slots
            if set(languages) & set(doctor_of(world, str(slot["doctor_id"])).get("languages", []))
        ]
    if isinstance(days, list) and days:
        slots = [slot for slot in slots if slot.get("agenda_day") in days]
    return slots


def window(slots, priority: str, now, rules):
    """Slots inside the priority deadline; the urgent reserve stays for U under rule P3."""

    deadline = deadline_of(priority, now)
    return [
        slot
        for slot in slots
        if (deadline is None or agenda_time(slot["agenda_date"]) < deadline)
        and not ("P3" in rules and priority != "U" and slot.get("urgent_reserve") is True)
    ]


def deadline_of(priority: str, now):
    if priority == "U":
        return now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    if priority in DEADLINE_DAYS:
        return now + timedelta(days=DEADLINE_DAYS[priority])
    return None


def doctor_of(world, doctor_id: str):
    for doctor in world.get("registry", {}).get("doctors", []):
        if doctor["doctor_id"] == doctor_id:
            return dict(doctor)
    return {}


def slot_by_id(world, slot_id: str):
    for slot in world.get("slots", []):
        if slot["slot_id"] == slot_id:
            return dict(slot)
    return {}


def number_for(market: Market, message: Message, suffix: str) -> str:
    raw = f"{market.agent_id}|{message.sender}|{message.need_id or message.msg_id}"
    core = hashlib.sha256(raw.encode()).hexdigest().upper()[:32]
    return f"prescription_number-{core}-{suffix}"


async def load(market: Market, message: Message):
    raw = await market.recall("need:" + (message.need_id or message.sender))
    state = json.loads(raw) if raw else {}
    return state if isinstance(state, dict) else {}


async def save(market: Market, message: Message, state) -> None:
    await market.remember("need:" + (message.need_id or message.sender), json.dumps(state))
