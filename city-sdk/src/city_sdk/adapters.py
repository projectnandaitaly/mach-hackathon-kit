"""Framework adapters. They name the City verbs and do not import those frameworks."""

from __future__ import annotations

from city_sdk.city import CITY_VERBS


def adk_city_tools() -> list[dict[str, str]]:
    return [{"name": name} for name in CITY_VERBS]


def langchain_city_tools() -> list[dict[str, str]]:
    return [{"name": name} for name in CITY_VERBS]


def mcp_city_tools() -> list[str]:
    return list(CITY_VERBS)
