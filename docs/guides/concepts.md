# Concepts

Short orientation for teams. Contest detail lives in the [care journey rules](../kit/e19-rules.md).

## Passive city

The city **registers**, **transports**, **observes**, and **enforces policy**. It does **not** choose a counterparty, rank Index results, or route a citizen for you. Your agent decides whom to ask.

## ASST and the five agents

Each team owns one **ASST** (Agency for Social Support in the Territory — a local health authority) and must field **five agents**, one per role:

| Role | `city init --role` | Card `role` |
|------|--------------------|-------------|
| Booking centre | `booking_centre` | `booking_centre` |
| Hospital | `hospital` | `facility` |
| GP | `gp` | `gp` |
| Specialist | `specialist_care` | `facility` |
| Family counseling | `family_counseling` | `facility` |

The city plays the citizens and holds cloud credentials. Teams compete to be the best ASST while City Hall raises difficulty.

## Harness vs brain

Every agent is **harness + brain**:

- **Harness** (city-provided, inside `city-sdk`): HTTP endpoints, signing, lease, fence, ack, journal.
- **Brain** (your code): an async function of a `Need` and a `City`. You import `city_sdk.city` only. You do not open sockets or sign messages.

## City

Day-1 team code uses [`city_sdk.city.City`](../kit/protocol.md): find peers, say, history, and Ledger chat. CAP wire detail stays inside the harness.

## Index, Relay, Ledger

| Piece | Role for you |
|-------|----------------|
| **Index** | Discovery: candidates for a service; **unordered** — you choose |
| **Relay / mailbox** | Every agent-to-agent message passes through the city |
| **Ledger** | World of the day: prescriptions, holds, commits, check-ins, reports, rules |

## Profiles

- **`cap/1.0`** — profile-neutral agent protocol (endpoints, envelopes, delivery).
- **`mhm/1.0` / care journey** — healthcare market vocabulary, roles, and scoring for this event.

## Speech and market acts

On the team edge, traffic is **chat text** (ADR-084; full detail in the kit):

- City verbs still name the act in the brain; the SDK sends `{"text": "..."}` with scalar `key=value` tokens
- Nested objects (for example a nested `doctor` claim) do not survive that render
- The citizen extracts scalar fields from the text; those fields drive choice against the registry

Fluency is not scored. The judge scores Ledger and world facts only.

## Next

Back to the linear path: [Getting started](getting-started.md) → [Develop agents](develop-agents.md) → [Levels](../kit/levels.md) → [Rules](../kit/e19-rules.md). Glossary: [glossary.md](glossary.md).
