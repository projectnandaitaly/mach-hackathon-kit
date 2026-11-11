# Facility (hospital, specialist, consultorio)

Three roles share the facility card shape. Scaffold each with its own `--role`:

```bash
city init agent hospital          --role hospital
city init agent specialist        --role specialist_care
city init agent family_counseling --role family_counseling
```

Card `role` is `facility`. The family is in `service_types` (`hospital.physiatry`, `specialist_care.cardiology`, `family_counseling.gynecology`).

## What the template already does

1. Offers its own slots from the Ledger `world` answer when asked.
2. Books with `hold slot_id=...` then `commit hold_id=... consent_ref=...`.
3. Records the visit with `checkin booking_id=... citizen=...`.
4. **Hospital / consultorio:** closes after check-in.
5. **Specialist:** files `report booking_id=... outcome_code=...` (and may issue a follow-up prescription), then closes after the report.
6. **Consultorio:** direct access without a prescription; stricter about personal data.

## Ledger acts you use

- `world`, `hold`, `commit`, `cancel`
- `checkin`, `report`
- optional follow-up: `prescribe … follow_up_of=<booking_id>`

See [templates/agent/SKILL.md](../../templates/agent/SKILL.md) and [Closing a case](e19-rules.md#closing-a-case-adr-106).
