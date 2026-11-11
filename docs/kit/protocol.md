# City, giorno 1

Il codice della squadra parla con la città tramite `city_sdk.city.City`.

Un **brain** è una funzione async di un `Need` e di una `City`. Trova i peer, può riordinarli con un proprio plugin, poi chiede, offre, ricorda o scala. L’**harness** serve l’agente: i brain non aprono connessioni e non firmano messaggi.

## Da qui

Leggere in ordine:

| Passo | Documento |
|------|-----|
| 1 | [Per iniziare](../guides/getting-started.md) |
| 2 | [Sviluppare agenti](../guides/develop-agents.md) |
| 3 | [Gli otto livelli](levels.md) |
| 4 | [Regole del care journey](e19-rules.md) |
| 5 | Note di ruolo sotto |

Appendice: [Concetti](../guides/concepts.md) · [Glossario](../guides/glossary.md)

## Ruoli

- [Medico di base (GP)](gp.md)
- [Centro prenotazioni (booking centre)](booking_centre.md)
- [Facility](facility.md) — ospedale, specialista, consultorio (`role: facility` più il service type)

## Linguaggio e atti

Nel brain i verbi della città nominano ancora l’atto; sul bordo squadra l’SDK invia solo testo di chat (ADR-084). I cittadini estraggono i campi scalari da quel testo. Gli oggetti annidati non sopravvivono sul filo. Vedere [Free text / chat sul bordo squadra](e19-rules.md#free-text--chat-sul-bordo-squadra-adr-084) nelle regole.
