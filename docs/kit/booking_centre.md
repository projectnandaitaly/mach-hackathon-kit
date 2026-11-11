# Centro prenotazioni (booking centre)

```bash
city init agent booking_centre
```

Poi impostate la card (e lo stesso ruolo in `city.yaml`):

```yaml
role: booking_centre
service_types:
  - booking_centre
```

## Cosa fa già il template

1. Verifica la ricetta con il Ledger (`prescription prescription_number=...`).
2. Chiede consenso e vincoli del cittadino.
3. Offre slot dalla risposta Ledger `world` entro la scadenza di priorità (attraversa altre ASST solo se la propria non ne ha).
4. Chiede alla facility di prenotare, poi inoltra la `booking.receipt`.
5. **Non** chiude dopo la ricevuta — la visita è ancora davanti.

Dal livello 4 in su le squadre di solito estendono matching linguistico, posti scarsi, liste d’attesa, rinnovi e ripensamenti.

## Atti Ledger che usate

- `prescription prescription_number=...`
- `world` (opzionale `procedure=`, `facility=`, `day=`, `page=`)
- Percorso facility: `hold` / `commit` avvengono sull’agente facility; voi inoltrate la ricevuta

Vedere [templates/agent/SKILL.md](../../templates/agent/SKILL.md) e [Gli otto livelli](levels.md).
