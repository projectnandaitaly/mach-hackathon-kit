"""Scripted role checks. The runner does not reorder Index candidates."""

from __future__ import annotations

from city_conform.agent import Agent
from city_conform.runner import Verdict, failed, passed
from city_conform.world import FIRST


async def b2b(agent: Agent) -> Verdict:
    before = len(agent.sent_log())
    await agent.request(
        "POST",
        "/invoke",
        agent.need(body={"service": "lab", "summary": "desk", "district": 8}),
    )
    sent = agent.sent_log()
    if len(sent) != before + 1 or sent[-1][0] != FIRST.agent_id:
        return failed("b2b send")
    return passed()
