# Family counseling

A free-access facility. No prescription is asked. Read a `Need`; `need.message` is the inbound message.

- `need.request` for a `family_counseling.*` service: `market.offer` the first free-access slot of that specialty from `market.world()`.
- `checkin`: `market.record_checkin`, then reply with `market.check_in`.
- Anything else: `Market.find_peers`, then `hold` the first peer.

When `need.is_emergency` is true, call `market.escalate` and stop.
