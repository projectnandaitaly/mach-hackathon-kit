"""Booking centre starter: verifies the prescription, offers slots inside the answer set, books.

Works as-is for the base journeys. The booking centre chooses among slots itself; the platform
ranks nothing. Offers come from the own ASST world first, and from facilities of other ASSTs only
when the own ASST has no slot that fits.

It does not send ``case.closed``: after the receipt the citizen still has the visit ahead, and a
close from the agent in charge would end the journey there.
"""

import json
from datetime import timedelta

from city_sdk.city import City, Message, Need, OutOfScope, agenda_time, clinical, understood

MAX_OFFERS = 3
MAX_PEERS = 3
DEADLINE_DAYS = {"B": 2, "D": 5}
FACILITY_FAMILIES = ("hospital", "specialist_care")
DAYS_KEYS = ("days", "available_days")
GENDER_KEYS = ("gender", "gender_preference")
BOOKING_FIELDS = ["available_days", "gender_preference"]
TAKEN = "slots:taken"


async def main(need: Need, city: City) -> None:
    if need.summary == "__error__":
        raise RuntimeError("application")
    if clinical(need.summary):
        raise OutOfScope()
    message = understood(need.message) if need.message is not None else None
    if need.is_emergency or (message is not None and message.body.get("is_emergency") is True):
        citizen = message.sender if message and message.sender.startswith("citizen:") else None
        await city.escalate(reason="red_flag", citizen=citizen, text="Sto segnalando un'emergenza.")
        return
    if message is None:
        return
    from_citizen = message.sender.startswith("citizen:")
    handler = (CITIZEN if from_citizen else FACILITY).get(message.type)
    if handler is None:
        return
    journey = await Journey.open(city, message)
    if not from_citizen and not journey.state:
        return
    await handler(journey)
    await journey.save()


class Journey:
    """One need as the booking centre sees it, kept in city memory under ``need:<need_id>``."""

    def __init__(self, city, message, state, world, now, rules, taken):
        self.city = city
        self.message = message
        self.state = state
        self.world = world
        self.now = now
        self.rules = rules
        self.taken = taken
        self.newly_taken = {}

    @classmethod
    async def open(cls, city: City, message: Message) -> "Journey":
        raw = await city.recall(_key(message))
        state = json.loads(raw) if raw else {}
        world = (await city.world()).get("world", {})
        now = await city.agenda_now()
        rules = {str(rule["rule_id"]) for rule in await city.active_rules()}
        return cls(city, message, state, world, now, rules, await _taken(city))

    async def save(self) -> None:
        if self.state:
            await self.city.remember(_key(self.message), json.dumps(self.state))
        if self.newly_taken:
            taken = await _taken(self.city)
            for slot_id, seats in self.newly_taken.items():
                taken[slot_id] = taken.get(slot_id, 0) + seats
            await self.city.remember(TAKEN, json.dumps(taken))

    def take(self, slot_id: str, seats: int = 1) -> None:
        """Seats of an own slot now booked or refused as full, shared by every journey."""

        self.newly_taken[slot_id] = self.newly_taken.get(slot_id, 0) + seats
        self.taken[slot_id] = self.taken.get(slot_id, 0) + seats

    @property
    def citizen(self) -> str:
        return str(self.state.get("citizen") or self.message.sender)


def _key(message: Message) -> str:
    return "need:" + (message.need_id or message.sender)


async def _taken(city: City) -> dict:
    raw = await city.recall(TAKEN)
    taken = json.loads(raw) if raw else {}
    return taken if isinstance(taken, dict) else {}


# --- from the citizen --------------------------------------------------------------------


async def on_need(journey: Journey) -> None:
    body = journey.message.body
    text = str(body.get("text") or "")
    if red_flag(text, journey.world):
        await journey.city.escalate(reason="red_flag", citizen=journey.message.sender)
        return
    waiting = journey.state.get("stage") in ("asked_consent", "asked_data")
    if waiting and not isinstance(body.get("prescription_number"), str):
        # Plain words while consent or data was asked: a refusal, or nothing to read. Offer
        # slots without the citizen's data rather than leave the citizen waiting.
        await search(journey)
        return
    area = body.get("area")
    district = area.get("district") if isinstance(area, dict) else body.get("district")
    number = body.get("prescription_number")
    journey.state.update(
        {
            "citizen": journey.message.sender,
            "prescription_number": number if isinstance(number, str) else None,
            "lang": body.get("lang") if isinstance(body.get("lang"), str) else None,
            "district": district if isinstance(district, int) else None,
        }
    )
    learn(journey, body)
    await start(journey)


async def on_data(journey: Journey) -> None:
    if not journey.state:
        return
    provided = journey.message.body.get("provided")
    provided = provided if isinstance(provided, dict) else {}
    learn(journey, provided)
    number = provided.get("prescription_number")
    if isinstance(number, str) and number != journey.state.get("prescription_number"):
        journey.state["prescription_number"] = number
        await start(journey)
        return
    if journey.state.get("stage") in ("asked_consent", "asked_data"):
        await search(journey)


async def on_confirm(journey: Journey) -> None:
    body = journey.message.body
    offer = journey.state.get("offers", {}).get(str(body.get("offer_id")))
    consent = body.get("consent_ref")
    if offer is None or not isinstance(consent, str):
        return
    pending = journey.state.get("pending")
    if isinstance(pending, dict) and pending.get("offer_id") == offer["offer_id"]:
        return
    if offer["offer_id"] in journey.state.get("refused", []):
        return
    journey.state["pending"] = {"offer_id": offer["offer_id"], "consent_ref": consent}
    if offer.get("foreign") and "P2" in journey.rules:
        await journey.city.ask_consent(journey.citizen, purpose="cross_asst")
        return
    await forward(journey, None)


async def on_consent(journey: Journey) -> None:
    consent = journey.message.body.get("consent_ref")
    if not isinstance(consent, str):
        return
    if journey.state.get("stage") == "asked_consent":
        journey.state["stage"] = "asked_data"
        await journey.city.request_data(
            journey.citizen, purpose="booking", fields=list(BOOKING_FIELDS)
        )
        return
    if journey.state.get("pending"):
        await forward(journey, consent)
        return
    if journey.state.get("stage") == "asked_data":
        # The citizen answered the data request with its consent again: search with what we have.
        await search(journey)


# --- from facilities ---------------------------------------------------------------------


async def on_offer(journey: Journey) -> None:
    body = dict(journey.message.body)
    request_id = str(body.pop("request_id", ""))
    last = body.pop("last", False) is True
    requests = journey.state.get("requests", {})
    if request_id not in requests or not asked(journey, request_id):
        return
    if fits(journey, body):
        row = {
            **body,
            "facility": journey.message.sender,
            "prescription_number": journey.state.get("prescription_number"),
            "foreign": True,
        }
        journey.state.setdefault("foreign", {})[str(row["slot_id"])] = row
        await present(journey, [row])
    if last:
        requests[request_id] = "done"
        await relax_when_done(journey)


async def on_cancel(journey: Journey) -> None:
    offer_id = str(journey.message.body.get("offer_id") or "")
    requests = journey.state.get("requests", {})
    if offer_id in requests:
        if asked(journey, offer_id):
            requests[offer_id] = "done"
            await relax_when_done(journey)
        return
    offer = journey.state.get("offers", {}).get(offer_id)
    if offer is None or offer.get("facility") != journey.message.sender:
        return
    journey.state.pop("pending", None)
    booked = journey.state.pop("booked", None)
    reason = journey.message.body.get("reason")
    fields = {"reason": reason if isinstance(reason, str) else "facility_cancelled"}
    if isinstance(booked, dict) and isinstance(booked.get("booking_id"), str):
        fields["booking_id"] = booked["booking_id"]
    await journey.city.decline(journey.citizen, offer_id=offer_id, **fields)
    excluded = journey.state.setdefault("excluded", [])
    if offer["slot_id"] not in excluded:
        excluded.append(offer["slot_id"])
    if reason == "NO_CAPACITY":
        journey.take(str(offer["slot_id"]), seats=capacity_of(journey.world, offer["slot_id"]))
    await search(journey)


async def on_receipt(journey: Journey) -> None:
    body = dict(journey.message.body)
    offer = journey.state.get("offers", {}).get(str(body.get("offer_id")))
    if offer is None or offer.get("facility") != journey.message.sender:
        return
    journey.state.pop("pending", None)
    journey.state["booked"] = {"offer_id": offer["offer_id"], "booking_id": body.get("booking_id")}
    journey.take(str(offer["slot_id"]))
    await journey.city.send_receipt(journey.citizen, body)


CITIZEN = {
    "need.request": on_need,
    "data.response": on_data,
    "confirm": on_confirm,
    "consent": on_consent,
}
FACILITY = {
    "offer": on_offer,
    "cancel": on_cancel,
    "booking.receipt": on_receipt,
}


# --- steps -------------------------------------------------------------------------------


async def start(journey: Journey) -> None:
    """Verify the prescription, then ask for the constraints the citizen discloses on request."""

    number = journey.state.get("prescription_number")
    if not isinstance(number, str):
        await renewal(journey)
        return
    try:
        record = await journey.city.verify_prescription(number)
    except Exception:
        await renewal(journey)
        return
    issued = record.get("prescription", {})
    if issued.get("patient") != journey.citizen:
        await renewal(journey)
        return
    if record.get("state") == "expired" and "P1" in journey.rules:
        await renewal(journey)
        return
    journey.state["procedure"] = issued["procedure"]
    journey.state["priority"] = issued["priority"]
    if "days" in journey.state or journey.state.get("asked"):
        await search(journey)
        return
    journey.state["asked"] = True
    journey.state["stage"] = "asked_consent"
    await journey.city.ask_consent(journey.citizen, purpose="booking")


async def renewal(journey: Journey) -> None:
    """Rule P1: send the citizen back to the GP for a valid prescription."""

    journey.state["stage"] = "renewal"
    await journey.city.request_data(
        journey.citizen, purpose="prescription_renewal", fields=["prescription_number"]
    )


async def search(journey: Journey) -> None:
    state = journey.state
    state["stage"] = "offered"
    state["presented"] = False
    if str(state["priority"]) == "P":
        await waiting_list(journey)
        return
    inside = [slot for slot in own_candidates(journey) if fits(journey, slot)]
    if inside:
        await present(journey, prefer(journey, inside)[:MAX_OFFERS])
        return
    if not await ask_partners(journey):
        await relax(journey)


async def ask_partners(journey: Journey) -> bool:
    """Ask facilities of other ASSTs for offers. Citizen data stays here until consent."""

    specialty = specialty_of(journey.world, str(journey.state["procedure"]))
    own = {row["agent_id"] for row in journey.world.get("registry", {}).get("facilities", [])}
    peers = []
    for family in FACILITY_FAMILIES:
        service = f"{family}.{specialty}"
        for found in await journey.city.find_facilities(service=service):
            if found.agent_id not in own and found.agent_id not in {p for p, _ in peers}:
                peers.append((found.agent_id, service))
    requests = journey.state.setdefault("requests", {})
    round_no = len(requests)
    sent = False
    for index, (peer, service) in enumerate(peers[:MAX_PEERS]):
        request_id = f"rq-{round_no + index + 1}"
        journey.state.setdefault("peers", {})[request_id] = peer
        fields = {
            "procedure": journey.state["procedure"],
            "priority": journey.state["priority"],
            "request_id": request_id,
        }
        if journey.state.get("lang"):
            fields["languages"] = [journey.state["lang"]]
        if journey.state.get("days"):
            fields["days"] = list(journey.state["days"])
        try:
            await journey.city.request_offers(peer, service_type=service, **fields)
        except Exception:
            requests[request_id] = "done"
            continue
        requests[request_id] = "open"
        sent = True
    return sent


def asked(journey: Journey, request_id: str) -> bool:
    """Only the facility a request went to may answer it."""

    peer = journey.state.get("peers", {}).get(request_id)
    return isinstance(peer, str) and peer == journey.message.sender


async def relax_when_done(journey: Journey) -> None:
    if any(value == "open" for value in journey.state.get("requests", {}).values()):
        return
    if journey.state.get("stage") == "offered" and not journey.state.get("presented"):
        await relax(journey)


async def relax(journey: Journey) -> None:
    """Empty answer set: no booking, and the closest alternatives with what they relax."""

    rows = []
    for slot in prefer(journey, own_candidates(journey))[:MAX_OFFERS]:
        rows.append({**slot, "relaxed": relaxed_of(journey, slot)})
    journey.state["stage"] = "relaxed"
    await present(journey, rows)


async def waiting_list(journey: Journey) -> None:
    now = journey.now.strftime("%Y-%m-%dT%H:%M:%SZ")
    row = {
        "offer_id": "wl-" + str(journey.state["prescription_number"]),
        "waiting_list": True,
        "procedure": journey.state["procedure"],
        "priority": "P",
        "slot": now,
        "expiry": now,
        "prescription_number": journey.state["prescription_number"],
    }
    await journey.city.present_offers(journey.citizen, [row])


async def present(journey: Journey, slots) -> None:
    if not slots:
        return
    rows = []
    offers = journey.state.setdefault("offers", {})
    refused = journey.state.get("refused", [])
    for slot in slots:
        offer_id = str(slot.get("offer_id") or "of-" + str(slot["slot_id"]))
        if offer_id in refused:
            offer_id = f"{offer_id}~{len(refused)}"
        doctor = slot.get("doctor") or doctor_claims(journey.world, str(slot["doctor_id"]))
        row = {
            "offer_id": offer_id,
            "slot_id": slot["slot_id"],
            "doctor_id": slot["doctor_id"],
            "facility": slot["facility"],
            "procedure": slot["procedure"],
            "agenda_date": slot["agenda_date"],
            "agenda_day": slot["agenda_day"],
            "doctor": doctor,
            "slot": slot["agenda_date"],
            "priority": journey.state["priority"],
            "expiry": slot["agenda_date"],
            "prescription_number": journey.state.get("prescription_number"),
            "relaxed": slot.get("relaxed") or None,
        }
        offers[offer_id] = {
            "offer_id": offer_id,
            "slot_id": slot["slot_id"],
            "facility": slot["facility"],
            "foreign": slot.get("foreign") is True,
        }
        rows.append({key: value for key, value in row.items() if value is not None})
    journey.state["presented"] = True
    await journey.city.present_offers(journey.citizen, rows)


async def forward(journey: Journey, cross_consent) -> None:
    """Ask the facility to book. Only the fields the hold needs leave the booking centre."""

    pending = journey.state["pending"]
    offer = journey.state["offers"][pending["offer_id"]]
    fields = {"slot_id": offer["slot_id"], "citizen": journey.citizen}
    if journey.state.get("prescription_number"):
        fields["prescription_number"] = journey.state["prescription_number"]
    if cross_consent is not None:
        fields["cross_asst_consent"] = cross_consent
    try:
        await journey.city.confirm(
            offer["facility"],
            offer_id=pending["offer_id"],
            consent_ref=pending["consent_ref"],
            **fields,
        )
    except Exception:
        await unreachable(journey, pending["offer_id"])


async def unreachable(journey: Journey, offer_id: str) -> None:
    """The city refused the send: the facility is down. Tell the citizen and offer again."""

    journey.state.pop("pending", None)
    journey.state.setdefault("refused", []).append(offer_id)
    await journey.city.decline(journey.citizen, offer_id=offer_id, reason="facility_unavailable")
    facility = journey.state["offers"][offer_id]["facility"]
    known = [
        row
        for row in journey.state.get("foreign", {}).values()
        if row["facility"] == facility
        and fits(journey, row)
        and agenda_time(row["agenda_date"]) >= journey.now
    ]
    if known:
        journey.state["stage"] = "offered"
        await present(journey, prefer(journey, known)[:MAX_OFFERS])
        return
    await search(journey)


# --- the answer set ----------------------------------------------------------------------


def own_future(journey: Journey):
    excluded = set(journey.state.get("excluded", []))
    return [
        dict(slot)
        for slot in journey.world.get("slots", [])
        if slot["procedure"] == journey.state["procedure"]
        and slot["slot_id"] not in excluded
        and journey.taken.get(slot["slot_id"], 0) < int(slot.get("capacity") or 1)
        and agenda_time(slot["agenda_date"]) >= journey.now
    ]


def capacity_of(world, slot_id) -> int:
    for slot in world.get("slots", []):
        if slot["slot_id"] == slot_id:
            return int(slot.get("capacity") or 1)
    return 1


def own_candidates(journey: Journey):
    """Own slots inside the priority deadline; under rule P3 the reserve is only for U."""

    priority = str(journey.state["priority"])
    deadline = deadline_of(priority, journey.now)
    return [
        slot
        for slot in own_future(journey)
        if (deadline is None or agenda_time(slot["agenda_date"]) < deadline)
        and not ("P3" in journey.rules and priority != "U" and slot.get("urgent_reserve"))
    ]


def fits(journey: Journey, slot) -> bool:
    """Hard constraints the citizen disclosed: language and available days."""

    if slot.get("slot_id") in set(journey.state.get("excluded", [])):
        return False
    return not relaxed_of(journey, slot)


def relaxed_of(journey: Journey, slot):
    missing = []
    lang = journey.state.get("lang")
    claimed = slot.get("doctor")
    if isinstance(claimed, dict):
        languages = list(claimed.get("languages", []))
    else:
        languages = list(doctor_of(journey.world, str(slot["doctor_id"])).get("languages", []))
    if lang and lang not in languages:
        missing.append("languages")
    days = journey.state.get("days")
    if isinstance(days, list) and days and slot.get("agenda_day") not in days:
        missing.append("days")
    return missing


def prefer(journey: Journey, slots):
    """Preferences order the set: gender, then district, then the earliest slot."""

    gender = journey.state.get("gender")
    district = journey.state.get("district")
    where = {
        row["agent_id"]: row.get("districts", [])
        for row in journey.world.get("registry", {}).get("facilities", [])
    }

    def key(slot):
        doctor = doctor_of(journey.world, str(slot["doctor_id"]))
        return (
            bool(gender) and doctor.get("gender") != gender,
            district is not None and district not in where.get(slot["facility"], []),
            agenda_time(slot["agenda_date"]),
            str(slot["slot_id"]),
        )

    return sorted(slots, key=key)


def learn(journey: Journey, body) -> None:
    for name in DAYS_KEYS:
        value = body.get(name)
        if isinstance(value, list) and all(isinstance(day, str) for day in value):
            journey.state["days"] = list(value)
            break
    for name in GENDER_KEYS:
        value = body.get(name)
        if value in ("f", "m"):
            journey.state["gender"] = value
            break


# --- world helpers -----------------------------------------------------------------------


def red_flag(text: str, world) -> bool:
    lowered = text.lower()
    return bool(lowered) and any(
        str(flag).lower() in lowered
        for row in world.get("protocols", [])
        for flag in row.get("red_flags", [])
    )


def deadline_of(priority: str, now):
    if priority == "U":
        return now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    if priority in DEADLINE_DAYS:
        return now + timedelta(days=DEADLINE_DAYS[priority])
    return None


def specialty_of(world, code: str) -> str:
    for row in world.get("catalogue", []):
        if row["code"] == code:
            return str(row["specialty"])
    return ""


def doctor_of(world, doctor_id: str):
    for doctor in world.get("registry", {}).get("doctors", []):
        if doctor["doctor_id"] == doctor_id:
            return dict(doctor)
    return {}


def doctor_claims(world, doctor_id: str):
    doctor = doctor_of(world, doctor_id)
    return {"languages": list(doctor.get("languages", [])), "gender": doctor.get("gender")}
