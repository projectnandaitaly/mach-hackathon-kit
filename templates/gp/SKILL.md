# Gp

Read a `Need`. Use `Market.remember` and `Market.recall` for the summary.

When the message carries a `symptom_code`, look it up in the protocols of `market.world()` and
`market.prescribe` the listed procedure and priority. A red flag in the summary escalates instead.

When `need.is_emergency` is true, call `market.escalate` and stop.
