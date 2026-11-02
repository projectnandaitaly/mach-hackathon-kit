"""Framework adapters. They name the Market verbs and do not import those frameworks."""

from __future__ import annotations

from city_sdk.market import MARKET_VERBS


def adk_market_tools() -> list[dict[str, str]]:
    return [{"name": name} for name in MARKET_VERBS]


def langchain_market_tools() -> list[dict[str, str]]:
    return [{"name": name} for name in MARKET_VERBS]


def mcp_market_tools() -> list[str]:
    return list(MARKET_VERBS)
