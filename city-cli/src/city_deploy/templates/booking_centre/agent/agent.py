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
    prescription_number = message.body.get("prescription_number") if message is not None else None
    if isinstance(prescription_number, str):
        await market.verify_prescription(prescription_number)
    # Inbound speech is available as need.message.text; citizens still decide from structured fields.
    peers = await market.find_facilities(service=need.service, district=need.district)
    ranked = await market.rank(peers, service=need.service)
    if ranked:
        await market.ask(ranked[0], need)
