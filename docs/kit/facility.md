# Facility (ospedale, specialista, consultorio)

Tre posti condividono la card facility. Create lo scaffold di una cartella, poi impostate `role: facility` e il service type di quel posto. Impostate anche `city.yaml` `role` a `facility`.

```bash
city init agent hospital
city init agent specialist
city init agent family_counseling
```

| Cartella | `capabilities` |
|--------|-----------------|
| `hospital` | `hospital.physiatry` |
| `specialist` | `specialist_care.cardiology` |
| `family_counseling` | `family_counseling.gynecology` |

## Cosa fa già il template

1. Offre i propri slot dalla risposta Ledger `world` quando richiesto.
2. Prenota con `hold slot_id=...` e poi `commit hold_id=... consent_ref=...`.
3. Registra la visita con `checkin booking_id=... citizen=...`.
4. **Specialista:** archivia `report booking_id=... outcome_code=...` (e può emettere una ricetta di follow-up), poi avvisa il cittadino.
5. **Consultorio:** accesso diretto senza ricetta; più rigoroso sui dati personali.

## Atti Ledger che usate

- `world`, `hold`, `commit`, `cancel`
- `checkin`, `report`
- follow-up opzionale: `prescribe … follow_up_of=<booking_id>`

Vedere [templates/agent/SKILL.md](../../templates/agent/SKILL.md) e [Quando il giudice valuta un journey](e19-rules.md#quando-il-giudice-valuta-un-journey-adr-119).
