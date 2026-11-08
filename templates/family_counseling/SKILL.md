# Family counseling

Works as-is for the base journeys. A free-access facility: no prescription is asked.

- `need.request` from a citizen: offer up to three own free-access slots.
- `confirm`: `market.hold` then `market.commit`, and send the receipt.
- `checkin`: `market.record_checkin`, reply with `market.check_in`, then `market.close_case`.

When `need.is_emergency` is true, call `market.escalate` and stop.
