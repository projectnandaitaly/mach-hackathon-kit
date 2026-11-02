from city_sdk.market import Market, Need, OutOfScope, clinical, understood


async def main(need: Need, market: Market) -> None:
    if need.summary == "__error__":
        raise RuntimeError("application")
    if clinical(need.summary):
        raise OutOfScope()
    if need.is_emergency:
        await market.escalate(reason="red_flag", text="I am escalating this red flag now.")
        return
    await market.remember("last", need.summary)
    await market.recall("last")
    message = understood(need.message) if need.message is not None else None
    symptom = message.body.get("symptom_code") if message is not None else None
    if message is None or not isinstance(symptom, str):
        return
    world = (await market.world())["world"]
    for row in world.get("protocols", []):
        if row["symptom_code"] != symptom:
            continue
        if any(flag in need.summary for flag in row.get("red_flags", [])):
            await market.escalate(reason="red_flag", text="A red flag matched the protocol.")
            return
        await market.prescribe(
            message.sender,
            procedure=row["procedure"],
            priority=row["priority"],
            symptom_code=symptom,
            text="Here is the prescription from the protocol table.",
        )
        return
