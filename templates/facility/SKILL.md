# Facility

Read a `Need`. Use `Market.find_peers`, then `hold` the first peer.

On a `checkin` message, call `market.record_checkin`, then reply with `market.check_in`.

When `need.is_emergency` is true, call `market.escalate` and stop.
