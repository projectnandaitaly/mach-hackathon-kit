# Medico di base (GP)

```bash
city init agent gp --role gp
```

## What the template already does

1. Refuses clinical free text (`OutOfScope`).
2. On a red flag / emergency: says `escalate.emergency reason=red_flag` to the citizen and stops.
3. Looks up the symptom in the protocol table (from the Ledger `world` answer or the built-in rows).
4. Issues the prescription: `prescribe patient=... procedure=... priority=...` to `city:ledger`.
5. Tells the citizen and **closes** with `status="closed"` (level 1 ends here).

Renewal of an expired prescription (P1 / later levels) is the main place teams extend this brain.

## Ledger acts you use

- `world` / protocol rows
- `prescribe patient=... procedure=... priority=...`
- `prescription prescription_number=...` when verifying

See [templates/agent/SKILL.md](../../templates/agent/SKILL.md) and [Closing a case](e19-rules.md#closing-a-case-adr-106).
