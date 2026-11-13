"""Starting brain. One turn, everything in ``main``.

Read the free text and who sent it, load this case and its actors, ask the
City LLM Gateway, ask the Index for agents, pick one, ask that agent, and
answer the citizen. The Index list is unordered; this brain picks.
"""

from __future__ import annotations

from city_sdk.city import LEDGER, CapError, City, Need


async def main(need: Need, city: City) -> None:
    # The message that opened this turn. Nothing of ours runs between messages.
    message = city.inbound
    if message is None:
        return

    # The free text. need.summary is the same words when the harness filled it in.
    text = message.text or need.summary
    if not text.strip():
        return

    # Who sent it. A citizen is citizen:..., an agent is agent:..., the Ledger is city:ledger.
    sender_id = message.sender
    # The Index does not return a display name. The name is the last piece of the id.
    sender_name = sender_id.split(":", 1)[-1].rsplit("/", 1)[-1]

    # This case only, oldest first. need_id leaves every other journey out.
    case = await city.history(need_id=message.need_id)
    # Every actor in that history: each sender and each recipient.
    # A citizen is citizen:..., an agent is agent:..., the Ledger is city:ledger.
    actors: list[str] = []
    for row in case:
        for party in (row.sender, row.recipient):
            if party and party not in actors:
                actors.append(party)

    # One completion through the City LLM Gateway, billed to the team.
    # messages is the OpenAI list of role and content. The reply text is content.
    # No team key raises KEY_UNKNOWN; this turn still continues.
    model_text = ""
    try:
        reply = await city.llm(
            [
                {
                    "role": "system",
                    "content": "You help a citizen. Reply in one short sentence.",
                },
                {
                    "role": "user",
                    "content": (
                        f"Actors in this case: {', '.join(actors) or sender_id}. "
                        f"The new message is: {text}"
                    ),
                },
            ],
            need_id=message.need_id,
        )
        content = reply["content"]
        if isinstance(content, str):
            model_text = content.strip()
    except CapError:
        model_text = ""

    # Agents whose role we can use. When the text names a capability, the card must list it.
    # The list is not ranked.
    agents = await city.find(text)

    # Role and capabilities live on the sender's card. A citizen has no card, so they stay empty.
    sender_role = ""
    sender_capabilities = ""
    for peer in agents:
        if peer.agent_id == sender_id:
            sender_role = peer.role
            sender_capabilities = ", ".join(peer.capabilities)
            break

    # An agent we asked has answered: forward the text to the citizen.
    # There is nothing to close. The judge scores the journey about a minute after
    # the citizen reaches its own end (prescription, receipt, check-in, or report).
    if sender_id.startswith("agent:"):
        # The citizen is whoever opened the case. They are one of the actors above.
        citizen = ""
        for row in case:
            if row.sender.startswith("citizen:"):
                citizen = row.sender
                break
        if citizen:
            who = sender_name if not sender_role else f"{sender_name} ({sender_role})"
            answer = f"{who} says: {text}"
            if model_text:
                answer = f"{answer} {model_text}"
            await city.say(citizen, answer)
        return

    # A new question comes from a citizen. Ledger lines and our own messages are not one.
    if not sender_id.startswith("citizen:"):
        return

    # find already left this agent out. Drop the Ledger too: asking it is a later step.
    choices = []
    for peer in agents:
        if peer.agent_id == LEDGER or peer.role == "ledger":
            continue
        choices.append(peer)
    if not choices:
        # Nothing to ask. Tell the citizen.
        await city.say(sender_id, "I could not find an agent for this.")
        return

    # The city does not choose. This brain asks the first peer in the list.
    chosen = choices[0]
    chosen_capabilities = ", ".join(chosen.capabilities)

    # Ask that agent, and say who wrote, in which role, with which capabilities.
    await city.say(
        chosen,
        (
            f"From {sender_name} ({sender_id})"
            f" role={sender_role or 'citizen'}"
            f" capabilities={sender_capabilities or '-'}: {text}"
        ),
    )

    # Tell the citizen who we asked. The agent's answer arrives later, as a new message.
    # When the gateway answered, that sentence is part of this reply.
    told = (
        f"I asked {chosen.agent_id}"
        f" (role {chosen.role or '-'}, capabilities {chosen_capabilities or '-'})."
    )
    if model_text:
        told = f"{told} {model_text}"
    await city.say(sender_id, told)
