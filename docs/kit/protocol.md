# City, day 1

Team code talks to the city through `city_sdk.city.City`.

A **brain** is an async function of a `Need` and a `City`. It finds peers, may reorder them with its own plugin, then asks, offers, remembers, or escalates. The **harness** serves the agent: brains do not open connections and do not sign messages.

## Start here

Read in order:

| Step | Doc |
|------|-----|
| 1 | [Getting started](../guides/getting-started.md) |
| 2 | [Develop agents](../guides/develop-agents.md) |
| 3 | [The eight levels](levels.md) |
| 4 | [Care journey rules](e19-rules.md) |
| 5 | Role notes below |

Appendix: [Concepts](../guides/concepts.md) · [Glossary](../guides/glossary.md)

## Roles

- [Medico di base (GP)](gp.md)
- [Centro prenotazioni (booking centre)](booking_centre.md)
- [Facility](facility.md) — hospital, specialist, consultorio (`role: facility` plus the service type)

## Speech and acts

City verbs still name the act in your brain; on the team edge the SDK sends chat text only (ADR-084). Citizens extract scalar fields from that text. Nested objects do not survive the wire. See [Free text / chat on the team edge](e19-rules.md#free-text--chat-on-the-team-edge-adr-084) in the rules.
