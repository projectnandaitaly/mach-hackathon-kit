# Gp

A `gp` agent starts from the single agent template (`city init agent gp --role gp`). Your brain looks the symptom up in the protocol table from the Ledger's `world` answer, issues the prescription with `prescribe patient=... procedure=... priority=...` to the Ledger, says `escalate.emergency` to the citizen on a red flag, and renews an expired prescription (P1). See [templates/agent/SKILL.md](../../templates/agent/SKILL.md).
