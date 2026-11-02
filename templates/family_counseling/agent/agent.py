from city_sdk.market import Market, Need, OutOfScope, agenda_time, clinical, understood

SERVICE = "family_counseling."
BRANCA = {"family_counseling.gynecology": "gynecology"}
PRIORITY = "P"


async def main(need: Need, market: Market) -> None:
    if need.summary == "__error__":
        raise RuntimeError("application")
    if clinical(need.summary):
        raise OutOfScope()
    if need.is_emergency:
        await market.escalate(reason="red_flag", text="I am escalating this red flag now.")
        return
    message = understood(need.message) if need.message is not None else None
    if message is not None and message.type == "checkin":
        booking_id = str(message.body["booking_id"])
        recorded = await market.record_checkin(booking_id=booking_id, citizen=message.sender)
        await market.check_in(
            message.sender,
            booking_id=booking_id,
            ledger_ref=recorded["ledger_ref"],
            agenda_time=recorded["agenda_time"],
            text="Check-in recorded. Welcome.",
        )
        return
    if message is not None and need.service.startswith(SERVICE):
        slot = await first_slot(market, BRANCA.get(need.service, ""))
        if slot is not None:
            await market.offer(
                message.sender,
                slot=slot["agenda_date"],
                priority=PRIORITY,
                expiry=slot["agenda_date"],
                offer_id="of-" + slot["slot_id"],
                slot_id=slot["slot_id"],
                doctor_id=slot["doctor_id"],
                facility=slot["facility"],
                procedure=slot["procedure"],
                agenda_date=slot["agenda_date"],
                agenda_day=slot["agenda_day"],
                text="Direct-access slot; no prescription required.",
            )
        return
    peers = await market.find_peers(service=need.service, district=need.district)
    if peers:
        await market.hold(peers[0])


async def first_slot(market: Market, specialty: str):
    """The first free-access slot of this specialty that has not started yet."""

    now = await market.agenda_now()
    world = (await market.world())["world"]
    codes = {
        row["code"]
        for row in world.get("catalogue", [])
        if row["specialty"] == specialty and not row["requires_prescription"]
    }
    for slot in world.get("slots", []):
        if slot["procedure"] in codes and agenda_time(slot["agenda_date"]) >= now:
            return dict(slot)
    return None
