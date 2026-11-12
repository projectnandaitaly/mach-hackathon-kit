"""Starting brain. One turn, everything in ``main``.

Read the free text and who sent it, ask the Index for agents, pick one, ask that
agent, and answer the citizen. The Index list is unordered; this brain picks.
"""

from __future__ import annotations

from city_sdk.city import LEDGER, City, Need


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

    # Close the case. The flag is status="closed" on city.say to the citizen.
    # It ends the journey and the judge scores it from this message.
    # The flag is accepted only on a message to a citizen. On an agent or the Ledger
    # the call is refused and nothing is sent. Only the agent the citizen wrote to
    # last can close. A later message to the citizen is ignored once the case is closed.
    # This turn is that close: an agent we asked has answered, so forward the text
    # to the citizen with the flag.
    if sender_id.startswith("agent:"):
        # The citizen is whoever opened the case. Read them from this case's history.
        case = await city.history(need_id=message.need_id)
        citizen = ""
        for row in case:
            if row.sender.startswith("citizen:"):
                citizen = row.sender
                break
        if citizen:
            who = sender_name if not sender_role else f"{sender_name} ({sender_role})"
            # status="closed" is the flag. The text is the final answer the judge reads.
            await city.say(citizen, f"{who} says: {text}", status="closed")
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
        # Nothing to ask. Close here with the same flag, status="closed".
        await city.say(sender_id, "I could not find an agent for this.", status="closed")
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

    # Tell the citizen who we asked. status="wip" keeps the case open.
    # The agent's answer arrives later, as a new message, and that turn sends status="closed".
    await city.say(
        sender_id,
        (
            f"I asked {chosen.agent_id}"
            f" (role {chosen.role or '-'}, capabilities {chosen_capabilities or '-'})."
        ),
        status="wip",
    )
