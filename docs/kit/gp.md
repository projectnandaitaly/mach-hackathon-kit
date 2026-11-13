# Medico di base (GP)

```bash
city init agent gp
```

Poi impostate la card (e lo stesso ruolo in `city.yaml`):

```yaml
role: gp
capabilities:
  - gp
```

## Cosa fa già il template

1. Rifiuta il free text clinico (`OutOfScope`).
2. Su red flag / emergenza: dice `escalate.emergency reason=red_flag` al cittadino e si ferma.
3. Cerca il sintomo nella tabella di protocollo (dalla risposta Ledger `world` o dalle righe built-in).
4. Emette la ricetta: `prescribe patient=... procedure=... priority=...` a `city:ledger`.
5. Informa il cittadino (al livello 1 il journey finisce qui).

Il rinnovo di una ricetta scaduta (P1 / livelli successivi) è il punto principale in cui le squadre estendono questo brain.

## Atti Ledger che usate

- `world` / righe di protocollo
- `prescribe patient=... procedure=... priority=...`
- `prescription prescription_number=...` in verifica

Vedere [templates/agent/SKILL.md](../../templates/agent/SKILL.md) e [Quando il giudice valuta un journey](e19-rules.md#quando-il-giudice-valuta-un-journey-adr-119).
