"""Replaceable layers. The default rank does not reorder candidates."""

from __future__ import annotations

from city_sdk.market import Peer, RankFn


def preserve_order(peers: list[Peer], *, service: str) -> list[Peer]:
    del service
    return list(peers)


class LayerRegistry:
    def __init__(self) -> None:
        self._rank: RankFn = preserve_order

    def rank_plugin(self) -> RankFn:
        return self._rank

    def swap_rank(self, plugin: RankFn) -> None:
        self._rank = plugin
