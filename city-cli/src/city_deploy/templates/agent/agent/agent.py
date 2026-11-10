"""Template agent for every role. The role is ``role`` in ``card.yaml``.

The brain runs once per inbound message. It can find agents, say something to an agent,
a citizen, or the Ledger, and read its own mailbox history. Nothing else reaches the city.
"""

import random

from city_sdk.city import LEDGER, City, Message, Need, OutOfScope, clinical

GREETING = "Hi, good morning! My name is Spencer, the booking center of ASST Bovina."


async def main(need: Need, city: City) -> None:
    message = city.inbound
    if message is None:
        return

    # Every message is free text: who wrote it, and what they wrote.
    sender = message.sender
    text = message.text or need.summary

    # card.yaml declares no_clinical_advice: refuse diagnosis and dosage requests.
    if clinical(text):
        raise OutOfScope()

    # This conversation with the sender in this case, oldest first, this message included.
    chat = await city.history(sender, need_id=message.need_id)

    if sender.startswith("citizen:"):
        await on_citizen(city, sender, text, chat)
    else:
        await on_agent(city, sender, text, message.need_id)


async def on_citizen(city: City, citizen: str, text: str, chat: list[Message]) -> None:
    if not any(row.sender == city.agent_id for row in chat):
        await city.say(citizen, GREETING)

    found = await city.find(f"a facility that can help with: {text}")
    agents = [peer for peer in found if peer.agent_id not in (city.agent_id, LEDGER)]
    if not agents:
        await city.say(citizen, "I could not find a facility for this request.", status="closed")
        return

    # The city never ranks; choosing is the agent's job. This template picks at random.
    chosen = random.choice(agents)
    await city.say(chosen, f"A citizen asks: {text}")
    await city.say(citizen, "I have asked a facility. I will write back with its answer.")


async def on_agent(city: City, agent: str, text: str, need_id: str | None) -> None:
    case = await city.history(need_id=need_id)
    citizen = next((row.sender for row in case if row.sender.startswith("citizen:")), None)
    if citizen is None:
        return
    # status="closed" ends the case: the judge scores the journey from this message.
    await city.say(citizen, f"{agent} answered: {text}", status="closed")
