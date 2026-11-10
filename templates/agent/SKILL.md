# Agent template

One template for every role. The role is `role` in `card.yaml` (`booking_centre`, `gp`, or `facility`; a facility's family comes from its `service_types`). `city init agent <name> --role <family>` writes both.

The brain runs once per inbound message and reaches the city through three verbs only:

| Step | SDK |
|---|---|
| Greet | `city.say(citizen, GREETING)` (a message to a citizen carries `status`, `wip` by default) |
| Read the free text | `city.inbound.text` |
| Know the sender | `city.inbound.sender` (`citizen:*`, `agent:*`, or `city:ledger`) |
| Refuse clinical requests | `if clinical(text): raise OutOfScope()` (the card declares `no_clinical_advice`) |
| Load the chat with that sender or case | `city.history(sender, need_id=...)`; `city.history(need_id=...)` for the whole case |
| Find agents | `city.find("<natural language>")` returns `list[Peer]`, unordered |
| Pick one at random | `random.choice(...)`, in team code: the city never ranks |
| Message that agent | `city.say(peer, text)` |
| Close the case with the citizen | `city.say(citizen, text, status="closed")`: the judge scores the journey from that message |

`city.llm(messages)` calls the team's model through the City LLM Gateway.

## The Ledger

The Ledger is an agent, `city:ledger` (`LEDGER` in `city_sdk.city`). Ask it in free text with `city.say(LEDGER, ...)`; its answer arrives as the next inbound message from `city:ledger`, in the same case. Write the identifiers as `key=value`:

- `world` (your ASST's slots, doctors, catalogue, and protocols; `procedure=`, `facility=`, `day=` narrow it, `page=` pages), `catalogue`
- `hold slot_id=...` (in the case of the message you answer), then `commit hold_id=... consent_ref=...`, or `cancel hold_id=...`
- `prescribe patient=... procedure=... priority=...` and `prescription prescription_number=...`
- `checkin booking_id=... citizen=...`, `report booking_id=... outcome_code=...`
- `remember key=... value=...`, `recall key=...`, `history need_id=...`, `agenda`, `rules`
- `help` lists every operation

A write without its identifiers is not done: the Ledger asks back. An emergency is said to the citizen with the token `escalate.emergency`.
