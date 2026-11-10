# Facility

The `hospital`, `specialist_care`, and `family_counseling` agents start from the single agent template (`city init agent <name> --role <family>`). Your brain offers its own slots from the Ledger's `world` answer, books with `hold slot_id=...` then `commit hold_id=... consent_ref=...` to the Ledger, records the check-in with `checkin`, and closes the case with `status="closed"` at the last step (after the check-in, or after the report for the specialist). See [templates/agent/SKILL.md](../../templates/agent/SKILL.md).
