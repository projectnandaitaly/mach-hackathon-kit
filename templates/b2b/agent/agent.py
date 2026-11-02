from city_sdk.market import Market, Need, OutOfScope, clinical


async def main(need: Need, market: Market) -> None:
    if need.summary == "__error__":
        raise RuntimeError("application")
    if clinical(need.summary):
        raise OutOfScope()
    if need.is_emergency:
        await market.escalate(reason="red_flag")
        return
    peers = await market.find_peers(service=need.service)
    ranked = await market.rank(peers, service=need.service)
    if ranked:
        await market.send(ranked[0], need)
