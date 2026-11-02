# Booking centre

Read a `Need`. When the message carries an `prescription_number`, call `market.verify_prescription` first.
Use `Market` to find a facility, keep the given order, and `ask` the first peer.

When `need.is_emergency` is true, call `market.escalate` and stop.
