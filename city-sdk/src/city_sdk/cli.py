"""In-process ``city run`` and ``city compare``."""

from __future__ import annotations

from city_sdk.harness import Brain
from city_sdk.layers import preserve_order
from city_sdk.market import Market, Need, Peer, RankFn
from city_sdk.ports import MemoryAttest, MemoryIndex, MemoryLedger


def compare(
    peers: list[Peer],
    plugin: RankFn,
    *,
    service: str = "",
) -> tuple[list[str], list[str]]:
    base = [peer.agent_id for peer in preserve_order(peers, service=service)]
    swapped = [peer.agent_id for peer in plugin(list(peers), service=service)]
    return base, swapped


async def run_scenario(
    brain: Brain,
    peers: list[Peer],
    need: Need,
    *,
    plugin: RankFn | None = None,
) -> list[str]:
    asked: list[str] = []
    relay = _RecordingRelay(asked)
    market = Market(
        index=MemoryIndex(peers=list(peers)),
        relay=relay,
        ledger=MemoryLedger(),
        attest=MemoryAttest(),
        rank=preserve_order if plugin is None else plugin,
        memory={},
        epoch=lambda: 0,
        traceparent=None,
    )
    await brain(need, market)
    return asked


class _RecordingRelay:
    def __init__(self, asked: list[str]) -> None:
        self._asked = asked

    async def send(
        self,
        agent_id: str,
        body: dict[str, object],
        *,
        traceparent: str | None,
    ) -> None:
        del body, traceparent
        self._asked.append(agent_id)
