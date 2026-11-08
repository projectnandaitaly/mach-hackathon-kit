# Hospital

Works as-is for the base journeys.

- `need.request` from a citizen: verify the prescription and offer up to three own slots inside the priority deadline. From a booking centre: offer own slots matching its criteria, or decline with `no_slot`.
- `confirm`: `market.hold` then `market.commit`, and send the receipt. A refusal is declined, never retried.
- `checkin`: `market.record_checkin`, reply with `market.check_in`, then `market.close_case`: the visit is the last step.

When `need.is_emergency` is true, call `market.escalate` and stop.
