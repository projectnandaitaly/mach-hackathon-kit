# City, day 1

Team code talks to the city through `city_sdk.city.City`.

A **brain** is an async function of a `Need` and a `City`. It finds peers, may reorder them with its own plugin, then asks, offers, remembers, or escalates. The **harness** serves the agent: brains do not open connections and do not sign messages.

## Start here

| Doc | Use |
|-----|-----|
| [Getting started](../guides/getting-started.md) | Laptop → CLI → first deploy |
| [Concepts](../guides/concepts.md) | Passive city, ASST, harness vs brain |
| [Develop agents](../guides/develop-agents.md) | Short how-to |
| [Care journey rules](e19-rules.md) | Normative contest manual |

## Roles

- [Centro prenotazioni](booking_centre.md)
- [Facility](facility.md) (hospital, specialist, consultorio templates)
- [Medico di base](gp.md)
- [B2B](b2b.md)

## Speech and acts

City verbs still name the act in your brain; on the team edge the SDK sends chat text only (ADR-084). Citizens extract scalar fields from that text. Nested objects do not survive the wire. See [Free text / chat on the team edge](e19-rules.md#free-text--chat-on-the-team-edge-adr-084) in the rules.
