"""The conforming booking_centre brain and the card the suite expects."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from city.testkit import ephemeral
from city_sdk.city import City, Need, OutOfScope, Peer
from city_sdk.harness import Brain, Harness
from city_sdk.ports import Clock, FixedClock, MemoryIndex, SystemClock

from city_conform.agent import LiveAgent, Sender

AGENT = "agent:7m2k9q4x8r1c/booking_centre"
TEAM = "team:t07"
SENDER = "agent:t05/osp-x"
CITY = "city:governance"
PROBER = "city:prober"
STRANGER = "agent:t99/stranger"
CLINICAL = re.compile(r"diagnos|dosage|posolog|\d+\s*mg", re.IGNORECASE)
FIRST = Peer("agent:t05/osp-x", service="lab", district=8)
SECOND = Peer("agent:t08/other", service="lab", district=8)


def clinical(text: str) -> bool:
    return CLINICAL.search(text) is not None


async def good_brain(need: Need, market: City) -> None:
    if need.summary == "__error__":
        raise RuntimeError("application")
    if clinical(need.summary):
        raise OutOfScope()
    inbound = market.inbound
    if need.is_emergency:
        if inbound is not None:
            await market.say(inbound.sender, "escalate.emergency reason=red_flag")
        return
    peers = await market.find(f"a facility for {need.service or 'a visit'}")
    if peers:
        await market.say(peers[0], need.summary or "request")


def profile_card(
    agent_id: str, public_x: str, *, role: str = "booking_centre", team: str = TEAM
) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "team": team,
        "name": role,
        "version": "0.9.0",
        "cap_versions": ["1.0"],
        "profiles": ["mhm/1.0"],
        "role": role,
        "service_types": ["lab"],
        "no_clinical_advice": True,
        "data_processing": [{"purpose": "care-coordination"}],
        "keys": [
            {
                "kid": agent_id + "#k1",
                "kty": "OKP",
                "crv": "Ed25519",
                "x": public_x,
            }
        ],
        "endpoints": {"mailbox": agent_id},
        "build": {"image_digest": "sha256:local", "git_sha": "dev", "sdk_version": "0.9.0"},
        "dependencies": {"models": []},
    }


async def good_agent(
    *,
    brain: Brain | None = None,
    card: dict[str, Any] | None = None,
    defer: bool = True,
    started: bool = True,
    clock: Clock | None = None,
    role: str = "booking_centre",
) -> LiveAgent:
    chosen = clock if clock is not None else SystemClock()
    agent = ephemeral()
    sender = ephemeral()
    city = ephemeral()
    prober = ephemeral()
    stranger = ephemeral()
    built = card if card is not None else profile_card(AGENT, agent.public.to_jwk()["x"], role=role)
    keys = {
        AGENT + "#k1": agent.public,
        SENDER + "#k1": sender.public,
        CITY + "#k1": city.public,
        PROBER + "#k1": prober.public,
    }
    harness = Harness(
        agent_id=AGENT,
        private=agent.private,
        public=agent.public,
        card=built,
        brain=good_brain if brain is None else brain,
        clock=chosen,
        keys=keys,
        defer=defer,
        index=MemoryIndex(peers=[FIRST, SECOND]),
    )
    if started:
        await harness.start()
    who: Sender = (SENDER, sender.private)
    return LiveAgent(
        harness,
        sender=who,
        prober=(PROBER, prober.private),
        city=(CITY, city.private),
        stranger=(STRANGER, stranger.private),
    )


def fixed_clock(moment: datetime) -> FixedClock:
    return FixedClock(moment)
