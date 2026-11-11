# Centro prenotazioni (booking centre)

```bash
city init agent booking_centre --role booking_centre
```

## What the template already does

1. Verifies the prescription with the Ledger (`prescription prescription_number=...`).
2. Asks consent and the citizen's constraints.
3. Offers slots from the Ledger `world` answer inside the priority deadline (crosses to other ASSTs only when its own has none).
4. Asks the facility to book, then forwards the `booking.receipt`.
5. Does **not** close after the receipt — the visit is still ahead.

From level 4 upward, teams usually extend language matching, scarce seats, waiting lists, renewals, and change-of-mind.

## Ledger acts you use

- `prescription prescription_number=...`
- `world` (optionally `procedure=`, `facility=`, `day=`, `page=`)
- Facility path: `hold` / `commit` happen on the facility agent; you forward the receipt

See [templates/agent/SKILL.md](../../templates/agent/SKILL.md) and [The eight levels](levels.md).
