"""Scripted role checks. The runner does not reorder Index candidates."""

from __future__ import annotations

from city_conform.agent import Agent
from city_conform.runner import Verdict, failed, passed
from city_conform.world import FIRST


async def booking_centre(agent: Agent) -> Verdict:
    sent_before = len(agent.sent_log())
    await agent.request(
        "POST",
        "/invoke",
        agent.need(
            body={"service": "lab", "summary": "alarm", "is_emergency": True, "district": 8}
        ),
    )
    if agent.escalation() != "red_flag" or len(agent.sent_log()) != sent_before:
        return failed("booking_centre emergency")
    await agent.request(
        "POST",
        "/invoke",
        agent.need(body={"service": "lab", "summary": "visit", "district": 8}),
    )
    sent = agent.sent_log()
    if len(sent) != sent_before + 1 or sent[-1][0] != FIRST.agent_id:
        return failed("booking_centre ask")
    return passed()


async def facility(agent: Agent) -> Verdict:
    before = len(agent.hold_log())
    await agent.request(
        "POST",
        "/invoke",
        agent.need(body={"service": "lab", "summary": "slot", "district": 8}),
    )
    holds = agent.hold_log()
    if len(holds) != before + 1 or holds[-1] != ("hold", FIRST.agent_id):
        return failed("facility hold")
    return passed()


async def gp(agent: Agent) -> Verdict:
    await agent.request(
        "POST",
        "/invoke",
        agent.need(body={"service": "lab", "summary": "follow-up", "district": 8}),
    )
    if agent.memory_view().get("last") != "follow-up":
        return failed("gp memory")
    return passed()


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
