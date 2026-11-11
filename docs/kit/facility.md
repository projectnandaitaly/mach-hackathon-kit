# Facility (hospital, specialist, consultorio)

Three seats share the facility card. Scaffold a folder, then set `role: facility` and the service type for that seat. Set `city.yaml` `role` to `facility` as well.

```bash
city init agent hospital
city init agent specialist
city init agent family_counseling
```

| Folder | `service_types` |
|--------|-----------------|
| `hospital` | `hospital.physiatry` |
| `specialist` | `specialist_care.cardiology` |
| `family_counseling` | `family_counseling.gynecology` |

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
