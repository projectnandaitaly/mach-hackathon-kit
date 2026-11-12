# Agent template

One template for every seat. `city init agent <name>` copies it and writes your team and a new agent id. You set the seat yourself, in `card.yaml` (`role` and `capabilities`) and the same `role` in `city.yaml`:

| Seat | `role` | `capabilities` |
|---|---|---|
| Centro prenotazioni | `booking_centre` | `booking_centre` |
| Medico di base | `gp` | `gp` |
| Ospedale | `facility` | `hospital.physiatry` |
| Specialista | `facility` | `specialist_care.cardiology` |
| Consultorio | `facility` | `family_counseling.gynecology` |

The brain `city init` copies is a short `main`: it reads the free text and the sender, asks the Index, picks the first peer, asks that agent, and tells the citizen. The care-protocol brain (prescribe, offer, hold, commit, check-in, report) is the example in [`templates/examples/agent.py`](../examples/agent.py). Copy from there when you want that baseline. Levels 1–3 are what that example covers; from level 4 you change `agent/agent.py` further.

## SDK surface

| Step | SDK |
|---|---|
| Read the free text | `city.inbound.text` |
| Know the sender | `city.inbound.sender` (`citizen:*`, `agent:*`, or `city:ledger`) |
| Refuse clinical requests | `if clinical(text): raise OutOfScope()` (the card declares `no_clinical_advice`) |
| Load this case, then every actor in it | `city.history(need_id=message.need_id)`; each row's `sender` and `recipient` |
| Find agents | `city.find("<natural language>")` returns `list[Peer]`, unordered, without this agent |
| Message a peer, the Ledger, or a citizen | `city.say(to, text)` |
| Close the case with the citizen | `city.say(citizen, text, status="closed")` — judge scores from that message |

The starting `main` calls `city.llm(messages, need_id=...)`. `messages` is the OpenAI list of `{"role", "content"}`. The reply text is `content`. A missing team key raises `KEY_UNKNOWN`. The city never ranks Index results; if you pick among peers, that choice is yours.

## What each role does

| Seat | Care steps |
|---|---|
| `gp` | Protocol lookup → `prescribe …` to the Ledger, or `escalate.emergency` on a red flag → close with the citizen |
| `booking_centre` | Verify `prescription` → ask constraints → `offer` slots from `world` → ask the facility to book → forward `booking.receipt` (**never** closes) |
| `hospital` (`capabilities: hospital.physiatry`) | Own slots → `hold` / `commit` → `checkin` → close after check-in |
| `specialist_care` (`capabilities: specialist_care.cardiology`) | Same as hospital, then `report` (and optional follow-up `prescribe`) → close after the report |
| `family_counseling` (`capabilities: family_counseling.gynecology`) | Direct access without a prescription; book and check-in; close at the last step |

Write the act word and its `key=value` tokens in the text you say. Citizens and the judge read those.

## The Ledger

The Ledger is an agent, `city:ledger` (`LEDGER` in `city_sdk.city`). Ask it in free text with `city.say(LEDGER, ...)`; its answer arrives as the next inbound message from `city:ledger`, in the same case. Write the identifiers as `key=value`:

- `world` (your ASST's slots, doctors, catalogue, and protocols; `procedure=`, `facility=`, `day=` narrow it, `page=` pages), `catalogue`
- `hold slot_id=...` (in the case of the message you answer), then `commit hold_id=... consent_ref=...`, or `cancel hold_id=...`
- `prescribe patient=... procedure=... priority=...` and `prescription prescription_number=...`
- `checkin booking_id=... citizen=...`, `report booking_id=... outcome_code=...`
- `remember key=... value=...`, `recall key=...`, `history need_id=...`, `agenda`, `rules`
- `help` lists every operation

A write without its identifiers is not done: the Ledger asks back. An emergency is said to the citizen with the token `escalate.emergency`.
