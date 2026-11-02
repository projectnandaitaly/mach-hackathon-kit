from city_sdk.market import Market, Need, OutOfScope, agenda_time, clinical, understood

SPECIALITY = "specialist_care."
OUTCOME = "VISITA-OK"


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
        await market.file_report(
            message.sender,
            booking_id=booking_id,
            outcome_code=OUTCOME,
            text="Visit complete; report filed.",
        )
        return
    if message is not None and need.service.startswith(SPECIALITY):
        prescription_number = message.body.get("prescription_number")
        if not isinstance(prescription_number, str):
            return
        issued = (await market.verify_prescription(prescription_number))["prescription"]
        slot = await first_slot(market, issued["procedure"])
        if slot is not None:
            await market.offer(
                message.sender,
                slot=slot["agenda_date"],
                priority=issued["priority"],
                expiry=slot["agenda_date"],
                offer_id="of-" + slot["slot_id"],
                slot_id=slot["slot_id"],
                doctor_id=slot["doctor_id"],
                facility=slot["facility"],
                procedure=slot["procedure"],
                agenda_date=slot["agenda_date"],
                agenda_day=slot["agenda_day"],
                prescription_number=prescription_number,
                text="First available slot for this procedure.",
            )
        return
    peers = await market.find_peers(service=need.service, district=need.district)
    if peers:
        await market.hold(peers[0])


async def first_slot(market: Market, procedure: str):
    """The first slot of this procedure that has not started yet."""

    now = await market.agenda_now()
    world = (await market.world())["world"]
    for slot in world.get("slots", []):
        if slot["procedure"] == procedure and agenda_time(slot["agenda_date"]) >= now:
            return dict(slot)
    return None
