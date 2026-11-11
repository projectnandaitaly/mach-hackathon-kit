# Concetti

Orientamento breve per le squadre. Il dettaglio del contest è nelle [regole del care journey](../kit/e19-rules.md).

## Città passiva

La città **registra**, **trasporta**, **osserva** e **applica le policy**. **Non** sceglie la controparte, non ordina i risultati dell’Index e non instrada un cittadino al posto vostro. È l’agente a decidere a chi chiedere.

## ASST e i cinque agenti

Ogni squadra gestisce un’**ASST** (Azienda Socio Sanitaria Territoriale — autorità sanitaria locale) e deve mettere in campo **cinque agenti**, uno per ruolo:

| Ruolo | `card.yaml` `role` | `service_types` |
|------|--------------------|-----------------|
| Centro prenotazioni | `booking_centre` | `booking_centre` |
| Ospedale | `facility` | `hospital.physiatry` |
| Medico di base | `gp` | `gp` |
| Specialista | `facility` | `specialist_care.cardiology` |
| Consultorio | `facility` | `family_counseling.gynecology` |

La città interpreta i cittadini e tiene le credenziali cloud. Le squadre competono per essere la miglior ASST mentre City Hall alza la difficoltà.

## Harness e brain

Ogni agente è **harness + brain**:

- **Harness** (fornito dalla città, dentro `city-sdk`): endpoint HTTP, firma, lease, fence, ack, journal.
- **Brain** (il vostro codice): una funzione async di un `Need` e di una `City`. Importate solo `city_sdk.city`. Non aprite socket e non firmate messaggi.

## City

Il codice della squadra al giorno 1 usa [`city_sdk.city.City`](../kit/protocol.md): trovare peer, parlare, storia e chat con il Ledger. Il dettaglio CAP resta nell’harness.

## Index, Relay, Ledger

| Pezzo | Ruolo per voi |
|-------|----------------|
| **Index** | Discovery: candidati per un servizio; **non ordinati** — scegliete voi |
| **Relay / mailbox** | Ogni messaggio tra agenti passa dalla città |
| **Ledger** | Mondo della giornata: ricette, hold, commit, check-in, referti, regole |

## Profili

- **`cap/1.0`** — protocollo agente indipendente dal profilo (endpoint, envelope, delivery).
- **`mhm/1.0` / care journey** — vocabolario del mercato sanitario, ruoli e punteggio di questo evento.

## Linguaggio e atti di mercato

Sul bordo squadra il traffico è **testo di chat** (ADR-084; dettaglio nel kit):

- Nel brain i verbi della città nominano ancora l’atto; l’SDK invia `{"text": "..."}` con token scalari `chiave=valore`
- Gli oggetti annidati (per esempio un claim `doctor` annidato) non sopravvivono a quel render
- Il cittadino estrae i campi scalari dal testo; su quelli decide rispetto al registry

La fluidità non è valutata. Il giudice valuta solo fatti di mondo e di Ledger.

## Avanti

Tornate al percorso lineare: [Per iniziare](getting-started.md) → [Sviluppare agenti](develop-agents.md) → [Livelli](../kit/levels.md) → [Regole](../kit/e19-rules.md). Glossario: [glossary.md](glossary.md).
