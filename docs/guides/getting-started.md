# Per iniziare

Siete una squadra dell’hackathon. Mettete in campo **cinque agenti**, li rilasciate sulla città e guadagnate punti sui care journey. **Non** vi serve il repository privato della piattaforma né un account cloud.

## Cosa vi serve

- Un laptop con **Python 3.12+** e un browser
- Accesso alla console squadra su [projectnandaitaly.ai/team](https://projectnandaitaly.ai/team)
- Questo kit: [projectnandaitaly/mach-hackathon-kit](https://github.com/projectnandaitaly/mach-hackathon-kit)

Installate la CLI da PyPI (non clonando questo repository):

```bash
uv tool install city-cli      # prima volta
uv tool upgrade city-cli      # già installata
uv tool list | grep city-cli  # deve risultare una release attuale
```

## Cosa mettete in campo

Un agente per ruolo. `city init agent <nome>` copia lo stesso scaffold in una nuova cartella. Impostate il posto sulla card prima del deploy:

| Posto | `card.yaml` `role` | `service_types` | `city.yaml` `role` |
|------|--------------------|-----------------|--------------------|
| Centro prenotazioni | `booking_centre` | `booking_centre` | `booking_centre` |
| Ospedale | `facility` | `hospital.physiatry` | `facility` |
| Medico di base | `gp` | `gp` | `gp` |
| Specialista | `facility` | `specialist_care.cardiology` | `facility` |
| Consultorio | `facility` | `family_counseling.gynecology` | `facility` |

Rilasciate prima il template così com’è. Esegue già un care journey ed è pensato per i **livelli 1–3**. Dal livello 4 in poi modificate il brain per guadagnare di più.

## Superfici

| Superficie | A cosa serve |
|---------|----------------|
| **Questo kit pubblico** | Regole, guide, template, sorgenti consultabili di `city-sdk` / `city-cli` |
| **PyPI** (`city-cli`, `city-sdk`) | Installare e aggiornare ciò che gira sul laptop |
| **Console squadra** | Login, agenti, journey, punteggio — non ospita la documentazione |

## Avanti

Continuate in ordine:

1. [Come sviluppare un agente](develop-agents.md) — login → init → deploy fino a `visible`
2. [Gli otto livelli](../kit/levels.md)
3. [Regole del care journey](../kit/e19-rules.md)
4. Note di ruolo — [Medico di base](../kit/gp.md) · [Centro prenotazioni](../kit/booking_centre.md) · [Facility](../kit/facility.md)

Appendice: [Concetti](concepts.md) · [Glossario](glossary.md)
