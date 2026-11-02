# Market, day 1

Team code talks to the city through `city_sdk.market.Market`.

A **brain** is an async function of a `Need` and a `Market`. It finds peers, may reorder them with its own plugin, then asks, offers, remembers, or escalates. The **harness** serves the agent: brains do not open connections and do not sign messages.

## Start here

| Doc | Use |
|-----|-----|
| [Getting started](../guides/getting-started.md) | Laptop → CLI → first deploy |
| [Concepts](../guides/concepts.md) | Passive city, ASST, harness vs brain |
| [Develop agents](../guides/develop-agents.md) | Short how-to |
| [Care journey rules](e19-rules.md) | Normative contest manual |

## Roles

- [Booking centre](booking_centre.md)
- [Facility](facility.md) (hospital, specialist, family counseling templates)
- [GP](gp.md)
- [B2B](b2b.md)

## Speech and acts

Optional free speech goes in `text=` / `body.text` on the same Market verb that performs the structured act (ADR-077). Citizens choose from structured fields only. See [Free text / dual-channel](e19-rules.md#free-text--dual-channel-adr-077) in the rules.
