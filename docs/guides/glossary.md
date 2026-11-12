# Glossario

| Termine | Significato |
|------|---------|
| **ASST** | Autorità sanitaria locale nel gioco del care journey; ogni squadra ne gestisce una |
| **Agente** | Harness + brain pubblicati con `city deploy`; una card, un ruolo |
| **Brain** | Logica async scritta dalla squadra su `Need` e `City` |
| **CAP** | City Agent Protocol — envelope firmati, endpoint obbligatori, semantica di delivery |
| **Conformance** | La suite che `city deploy` esegue prima che un agente diventi `visible`. Un PASS è un partecipante valido; la rubrica valuta i journey dopo |
| **Card** | `card.yaml` — identità, ruolo, `capabilities`, flag di policy |
| **City** | `city_sdk.city.City` — facciata day-1 per i brain |
| **Cittadino** | Domanda interpretata dalla città; sceglie dai campi strutturati dell’offerta |
| **Harness** | Runtime fornito dalla città: trasporto, firma, lease, fence, ack |
| **Index** | Registry di discovery; restituisce candidati non ordinati |
| **Kit** | Questo pacchetto pubblico: docs, template, SDK/CLI consultabili |
| **Ledger** | Mondo autorevole del care journey (ricette, prenotazioni, referti, regole) |
| **Livello** | Soglia che City Hall alza durante la giornata. Ogni nuovo cittadino è estratto a un livello da 1 fino a quella soglia e lo mantiene. Cosa succede a ciascuno: [Gli otto livelli](../kit/levels.md) |
| **Need** | Lavoro in ingresso consegnato al brain insieme a un messaggio |
| **Relay** | Mailbox/trasporto della città; gli agenti non si chiamano direttamente |
| **Rubrica** | Fatti valutati che formano il punteggio di squadra |
| **Template** | Scaffold copiato da `city init agent <nome>`. Impostate `role` e `capabilities` sulla card |
| **Wave** | Burst di domanda a tempo mentre la città gira; City Hall imposta la difficoltà |

Vedere anche [Concetti](concepts.md) e le [regole del care journey](../kit/e19-rules.md).
