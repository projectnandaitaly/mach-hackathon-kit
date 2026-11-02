# B2b

Read a `Need`. Use `Market.find_peers`, keep the given order, and `send` to the first peer.

When `need.is_emergency` is true, call `market.escalate` and stop.
