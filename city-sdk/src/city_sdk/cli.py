"""In-process ``city run``: one brain turn against local stand-ins."""

from __future__ import annotations

from city_sdk.city import City, Message, Need, Peer
from city_sdk.harness import Brain
from city_sdk.ports import LocalLedger, MemoryIndex, MemoryRelay


async def run_scenario(
    brain: Brain,
    peers: list[Peer],
    need: Need,
    *,
    inbound: Message | None = None,
    agent_id: str = "agent:local/dev",
) -> list[str]:
    """The parties the brain said something to, in order."""

    relay = MemoryRelay(agent_id=agent_id, ledger=LocalLedger())
    message = inbound if inbound is not None else need.message
    if message is not None:
        relay.record(message)
    market = City(
        index=MemoryIndex(peers=list(peers)),
        messages=relay,
        history=relay,
        agent_id=agent_id,
        inbound=message,
    )
    await brain(need, market)
    return [str(row["to"]) for row in relay.posted]
