# Consultorio

Works as-is for the base journeys. A free-access facility: no prescription is asked.

- `need.request` from a citizen: offer up to three own free-access slots.
- `confirm`: `city.hold` then `city.commit`, and send the receipt.
- `checkin`: `city.record_checkin`, reply with `city.check_in`, then `city.close_case`.

When `need.is_emergency` is true, call `city.escalate` and stop.
