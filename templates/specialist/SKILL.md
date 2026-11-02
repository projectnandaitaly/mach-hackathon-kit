# Specialist

A specialist_care facility. Read a `Need`; `need.message` is the inbound message.

- `need.request` with an `prescription_number`: `market.verify_prescription`, then `market.offer` the first slot of that procedure from `market.world()`.
- `checkin`: `market.record_checkin`, reply with `market.check_in`, then `market.file_report` with the outcome.
- Anything else: `Market.find_peers`, then `hold` the first peer.

When `need.is_emergency` is true, call `market.escalate` and stop.
