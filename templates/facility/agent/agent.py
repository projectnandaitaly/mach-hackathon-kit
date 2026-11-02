from city_sdk.market import Market, Need, OutOfScope, clinical, understood


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
    peers = await market.find_peers(service=need.service, district=need.district)
    if peers:
        await market.hold(peers[0])
