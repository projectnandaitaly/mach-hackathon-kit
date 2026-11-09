from city_sdk.city import City, Need, OutOfScope, clinical


async def main(need: Need, city: City) -> None:
    if need.summary == "__error__":
        raise RuntimeError("application")
    if clinical(need.summary):
        raise OutOfScope()
    if need.is_emergency:
        await city.escalate(reason="red_flag")
        return
    peers = await city.find_peers(service=need.service)
    ranked = await city.rank(peers, service=need.service)
    if ranked:
        await city.send(ranked[0], need)
