"""GP starter: applies the ASST protocol table and never reasons clinically.

Works as-is for the base journeys: a symptom gets the prescription from the protocol table, a red
flag is escalated, an expired prescription is renewed (rule P1). Edit freely; keep the Market calls.
"""

from city_sdk.market import Market, Message, Need, OutOfScope, clinical, understood

CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
# Plain-language words a citizen may use instead of a symptom code.
KEYWORDS = {
    "SYM-BACK": ("mal di schiena", "back pain"),
    "SYM-KNEE": ("ginocchio", "knee"),
    "SYM-PALPITATIONS": ("cuore che batte", "heart racing"),
    "SYM-FOLLOWUP": ("controllo del cuore", "routine heart check"),
}


async def main(need: Need, market: Market) -> None:
    if need.summary == "__error__":
        raise RuntimeError("application")
    if clinical(need.summary):
        raise OutOfScope()
    message = understood(need.message) if need.message is not None else None
    citizen = message.sender if message and message.sender.startswith("citizen:") else None
    if need.is_emergency or (message is not None and message.body.get("is_emergency") is True):
        await market.escalate(reason="red_flag", citizen=citizen, text="I am escalating this now.")
        if citizen is not None:
            await market.close_case(citizen, text="Emergency services have your case.")
        return
    if message is None or citizen is None or message.type != "need.request":
        return
    body = message.body
    symptom = body.get("symptom_code")
    prescription_number = body.get("prescription_number")
    text = str(body.get("text") or "")
    protocols = (await market.world()).get("world", {}).get("protocols", [])
    if red_flag(text, protocols):
        await market.escalate(reason="red_flag", citizen=citizen, text="This needs urgent care.")
        await market.close_case(citizen, text="Emergency services have your case.")
        return
    row = protocol(protocols, symptom, text)
    if row is not None:
        await market.prescribe(
            citizen,
            procedure=str(row["procedure"]),
            priority=str(row["priority"]),
            symptom_code=str(row["symptom_code"]),
            prescription_number=number_for(message),
            text="Here is your prescription. Book it with the booking centre.",
        )
        # The citizen moves on to the booking centre; the city ignores this close if it has.
        await market.close_case(citizen, text="My part is done.")
        return
    if isinstance(prescription_number, str):
        await renew(market, message, prescription_number)


def red_flag(text, protocols):
    lowered = text.lower()
    return bool(lowered) and any(
        str(flag).lower() in lowered for row in protocols for flag in row.get("red_flags", [])
    )


def protocol(protocols, symptom, text):
    """The row for a symptom code, or the first row whose keyword appears in the text."""

    for row in protocols:
        if row["symptom_code"] == symptom:
            return row
    if isinstance(symptom, str):
        return None
    lowered = text.lower()
    for row in protocols:
        words = row.get("keywords") or KEYWORDS.get(str(row["symptom_code"]), ())
        if any(str(word).lower() in lowered for word in words):
            return row
    return None


async def renew(market: Market, message: Message, prescription_number: str) -> None:
    """Rule P1: an expired prescription is reissued with the same procedure and priority."""

    try:
        record = await market.verify_prescription(prescription_number)
    except Exception:
        return
    issued = record.get("prescription", {})
    if record.get("state") != "expired" or issued.get("patient") != message.sender:
        return
    await market.prescribe(
        message.sender,
        procedure=str(issued["procedure"]),
        priority=str(issued["priority"]),
        prescription_number=number_for(message, "R"),
        text="Your prescription was expired; here is a new one.",
    )


def number_for(message: Message, suffix: str = "") -> str:
    """A prescription number derived from the citizen and the journey, so a replay matches."""

    who = message.sender.split(":", 1)[-1]
    raw = f"{who}-{message.need_id or message.msg_id}".upper()
    core = "".join(char for char in raw if char in CHARS)[:34]
    return "prescription_number-" + core + (f"-{suffix}" if suffix else "")
