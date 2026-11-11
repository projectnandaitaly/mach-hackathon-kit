# Gli otto livelli, come accadono

Queste scene raccontano come si sente un care journey mentre la città gira. Punteggio e campi dei messaggi restano nelle [regole del care journey](e19-rules.md). Le scene seguono quelle regole.

City Hall alza una **soglia** durante la giornata. Il numero sul banner è quella soglia. Ogni nuovo cittadino è estratto a un livello da 1 fino alla soglia, con il numero di livello come peso, e lo mantiene per tutto il journey. Con soglia 4 i pesi sono 1, 2, 3 e 4: un nuovo arrivo ha quattro volte più probabilità di arrivare al livello 4 che al livello 1. Alzare la soglia cambia chi arriva dopo. Un journey già aperto tiene il livello con cui è partito.

Ogni ASST incontra lo stesso cittadino allo stesso istante di agenda, con i propri medici e i propri ambulatori. Un giorno di agenda sono sei minuti reali. Arriva circa un cittadino ogni due minuti reali, e l’agenda è quieta il sabato e la domenica.

Dentro un livello estratto la città sceglie una persona che quel livello è abbastanza alto da includere. Un’estrazione a 4 può ancora essere Giulia con il mal di schiena. Amir, che serve una lingua dell’ASST vicina, compare solo dall’estrazione 4 in su. Più alta è la soglia, più spesso le persone difficili sono in coda. La [rubrica](e19-rules.md#rubrica) non cambia.

I cinque brain del template, lasciati come li ha scritti `city init`, portano i cittadini ai livelli 1, 2 e 3 e guadagnano le righe base. Dal livello 4 quegli stessi brain rispondono nel modo più semplice e lasciano punti per strada. Quello è lo spazio per la vostra strategia.

Tenete tutti e cinque gli agenti sul mercato mentre la soglia è ancora bassa. Un cittadino che raggiunge un medico di base, un centro prenotazioni o un consultorio vuoti termina `unserved`, e la squadra perde 4 punti per quel cittadino.

## Chi entra

| Cittadino | Dal livello | Cosa porta |
|---|---|---|
| Giulia | 1 | Mal di schiena (`SYM-BACK`). Italiano. Protocollo: fisiatria `VIS-FIS`, priorità **B** (entro 2 giorni di agenda). |
| Paolo | 1 | Palpitazioni che sono red flag: dolore al petto da stamattina. |
| Lea | 1 | Palpitazioni senza red flag (`SYM-PALPITATIONS`). Cardiologia `VIS-CARD`, priorità **B**. Più avanti si aspetta un referto. |
| Luca | 2 | Ginocchio dolente (`SYM-KNEE`). Fisiatria, priorità **D** (entro 5 giorni di agenda). Preferisce un medico uomo il giovedì o il venerdì. |
| Elena | 2 | Mal di schiena, e vuole solo un medico che parli inglese. |
| Chiara | 3 | Parte dal consultorio (`CONS-GIN`). Non ha ricetta. |
| Marta | 3 | Palpitazioni, come Lea, e rifiuta di condividere il referto. |
| Amir | 4 | Scrive in inglese. Il medico che accetta parla una lingua che nessuno dei vostri parla. |
| Sofia | 5 | Un fisiatra uomo, e solo il venerdì. |
| Nora | 5 | Una fisiatra donna il giovedì o il venerdì. |
| Pietro | 5 | Un follow-up (`SYM-FOLLOWUP`). Cardiologia, priorità **P**. Può aspettare. |
| Sara | 6 | Prenota e, un’ora di agenda dopo, cambia idea. |
| Renato | 8 | Chiede al centro prenotazioni una visita senza ricetta. |
| Ada | 8 | Prende la ricetta e aspetta due giorni e mezzo di agenda prima di chiamare. |

I nomi qui sono un cast per le scene. Sul filo un cittadino è un `citizen_id` e una persona, e le parole arrivano dalla banca di utterance congelata.

## Livello 1. La tabella sulla scrivania del medico

Giulia scrive al vostro medico di base. Il messaggio è strutturato. Il codice è `SYM-BACK`, la lingua è l’italiano e la frase descrive una schiena che fa male. Non c’è `is_emergency` sul suo messaggio. La tabella di protocollo sul Ledger ha già la sua riga: fisiatria `VIS-FIS` a priorità B. Il medico di base legge quella riga, la prescrive, e la giornata di Giulia finisce con la ricetta in mano. Al livello 1 la ricetta è l’intero journey.

Paolo scrive nella stessa ora. Il suo codice è `SYM-PALPITATIONS`, che da solo sarebbe una visita cardiologica, e `is_emergency` è vero. La frase dice che ha dolore al petto da stamattina. Il dolore al petto è la red flag di quella riga di protocollo. Il medico di base scala, motivo `red_flag`. Paolo non riceve ricetta. Il journey termina escalato e il giudice non scrive penalità.

Lea porta lo stesso codice di Paolo e nessuna red flag. Esce con `VIS-CARD` a priorità B. A un livello successivo siederà con il vostro specialista per il referto. Oggi la ricetta chiude il suo caso.

Quando quella ricetta o quella escalation è l’ultima parola, il medico di base chiude il caso e il giudice parte subito. Una procedura o una priorità sbagliata vale −1. Se il medico di base non scala Paolo, il giudice scrive −2. Una frase che dice solo di chiamare il 112, senza l’atto `escalate.emergency`, non è un’escalation: il giudice legge la parola d’atto e i token `chiave=valore`, come fa `understood()`.

**La morale.** La tabella di protocollo è l’intera risposta clinica. Prescrivete la sua riga, o scalate la red flag, e questo cittadino è a posto.

## Livello 2. La ricetta cammina fino allo sportello

Giulia torna, e questa volta la ricetta è ciò che porta dopo. Porta il `prescription_number` al vostro centro prenotazioni. Il centro la verifica sul Ledger, chiede la prenotazione e offre posti che la città ha già contato nel suo insieme di risposta: fisiatria, entro la scadenza di priorità, con un medico che parla italiano. Aspetta trenta minuti di agenda dopo la prima offerta, circa sette secondi reali, poi sceglie. Mandate le offerte insieme. Conferma. L’ospedale tiene il posto e lo committa. La ricevuta in mano è quella firmata dal Ledger. Al livello 2 si ferma su quella ricevuta. La visita stessa è valutata dal livello 3, quando resta per il check-in.

Una ricevuta senza commit dietro è una prenotazione fantasma, −2.

Luca ha il ginocchio dolente. Preferirebbe un medico uomo il giovedì o il venerdì. È una preferenza: una prenotazione che la rispetta vale +2, una solo nell’insieme più ampio vale +1. I suoi giorni e la preferenza tornano quando il centro prenotazioni chiede, con uno scopo che lui accetta. Offrite un posto che combacia, e lo slot che conferma può cadere nell’insieme preferito. Il primo posto legale, senza riguardo per il giovedì o il genere del medico, resta una prenotazione legale da +1. I template prendono quella via più semplice.

Elena vuole solo un medico che parli inglese. Uno slot solo in italiano è fuori dal suo insieme, e lo rifiuta con un motivo. Il vostro ospedale ha un fisiatra che parla inglese, quindi può essere prenotata in casa.

I posti che un altro cittadino ha già committato escono dall’insieme prima del giudizio. Perdere quella corsa lascia il punteggio intatto.

Da questo livello in poi, la chiusura del medico di base dopo la ricetta è ignorata: Giulia è già andata al centro prenotazioni, e il centro non deve chiudere dopo la ricevuta perché la visita è ancora davanti.

**La morale.** Verificate la ricetta, offrite posti che esistono e consegnate la ricevuta del Ledger.

## Livello 3. La città inizia a parlare

Il primo messaggio di Giulia ha le sue parole e nessun `symptom_code`. Il medico di base legge la frase, trova la riga del mal di schiena e la prescrive. Anche il dolore al petto di Paolo è nella frase. Il campo `is_emergency` è impostato solo sulle red flag strutturate dei livelli 1 e 2: da qui l’allarme sono le parole stesse.

Il journey ora passa dalla visita. Dopo la ricevuta Giulia fa check-in all’ospedale nella mezza giornata dello slot, mattina o pomeriggio di agenda. L’ospedale registra quel check-in contro la ricevuta valida: +2, e +1 perché lo slot era davvero committato. Rifiutare una ricevuta valida vale −2.

Lea siede con lo specialista. Dopo il check-in lo specialista chiede consenso con scopo `report_sharing`, archivia il referto sotto il `consent_ref` restituito e può aggiungere una ricetta di follow-up quando il journey ne ha bisogno. La tabella di protocollo non ha una colonna follow-up. Quella scelta è dello specialista. Un referto archiviato con il follow-up che il caso richiede vale +2. Un referto mancante vale −1.

Marta è Lea, e dice no alla condivisione del referto. Il rifiuto di solito arriva in parole semplici, senza nome d’atto. `understood()` allora legge un `need.request` senza campi. Trattate il campo mancante come un no, archiviate il referto non condiviso e chiudete. Aspettare un consenso che non arriverà tiene fermo il journey fino al timeout.

Chiara non scrive mai al medico di base. Scrive al consultorio, accesso libero, procedura `CONS-GIN`, nessun `prescription_number`. Prenotatela e fatele fare check-in. Chiederle un numero di ricetta la fa tacere: il journey termina senza risposta, chiedere una ricetta vale −1, e prenotarla senza vale +2.

Lingua, distretto e numero di ricetta sono sulla richiesta di prenotazione dall’inizio. Giorni e preferenza di genere tornano solo dopo un `data.request` il cui scopo il cittadino accetta. Quando la risposta è un rifiuto in linguaggio naturale, offrite gli slot senza i dati che avete chiesto.

**La morale.** Leggete la frase, onorate il check-in, archiviate il referto anche se non lo condivide, e aprite il consultorio senza ricetta.

## Livello 4. La lingua della porta accanto

Ad Amir fa male la schiena. Scrive in inglese. La lingua che accetta è una scarsa, estratta quando Play apre il mondo e fissa per la run: una per ASST, da un elenco che passa per arabo, cinese, spagnolo, rumeno e il resto. In ogni ASST sta su un solo fisiatra, un uomo che lavora martedì e giovedì. Il vostro fisiatra anglofono ha la lingua sbagliata per lui. Il medico con quella giusta appartiene a un’altra ASST, e quale ASST dipende dal livello a cui Amir è stato estratto. Leggete le lingue dei medici sotto **ASST world**. Quando l’ospedale del partner è sul mercato, il suo insieme di risposta vive interamente di là. Quando è vuoto, chiede invece la vostra lingua scarsa, e il fisiatra del martedì e giovedì può vederlo.

Il centro prenotazioni guarda in casa, non trova un posto nella sua lingua e chiede all’ospedale partner. La richiesta che attraversa porta la ricerca: servizio, procedura, priorità, lingua. Il suo id cittadino, il numero di ricetta, il distretto e il free text sulla schiena restano dalla vostra parte dell’anello. Il partner offre i propri slot. Il vostro centro li presenta, lui conferma, il partner committa e fa check-in attraverso l’anello. I punti di prenotazione restano al vostro centro. I punti di check-in appartengono alla facility partner.

Ogni offerta che vede è controllata contro il registry: medico, facility, procedura, giorno, lingua, genere. Uno slot vestito con una lingua che il medico non parla è un attributo falso, −2, e non lo prende.

I template offrono i posti più precoci e attraversano nel modo più semplice. Amir è dove iniziano a lasciare punti per strada.

**La morale.** Quando il posto è di porta accanto, attraversate l’anello con una ricerca e senza dossier, e lasciate che l’offerta ripeta il registry parola per parola.

## Livello 5. Tre modi in cui il calendario dice no

Sofia vuole un fisiatra uomo, e solo il venerdì. Quel posto esiste. Un fisiatra in ospedale è uomo e lavora mercoledì, giovedì e venerdì. Il centro chiede, lei risponde venerdì e `m`, e l’offerta è quel venerdì. Una prenotazione nel suo insieme preferito vale +2. Fa check-in.

Nora vuole una donna, il giovedì o il venerdì. Girate l’anello e il posto manca. La donna in fisiatria lavora da lunedì a mercoledì. I fisiatri di giovedì e venerdì sono uomini. L’insieme di risposta di Nora è vuoto. Non prenotate nulla. Offrite un’alternativa rilassata, un posto reale nel giorno sbagliato o con il medico sbagliato, e lasciate che lo rifiuti. Invia un cancel con motivo `gender`. Nessuna prenotazione, insieme a un’alternativa reale, vale 0. Il silenzio a insieme vuoto, o una prenotazione fuori insieme, è la perdita.

Pietro è un follow-up. Il codice è `SYM-FOLLOWUP`, la visita è cardiologia, la priorità è **P**, e P significa dopo. L’offerta che aspetta è una lista d’attesa. La accetta e il journey termina sulla lista, senza prenotazione. Quella fine vale 0, la stessa riga calma dell’alternativa di Nora. Un cittadino a priorità P spinto in uno slot fuori dal suo insieme è l’esito sbagliato.

**La morale.** Prenotate dentro l’insieme. Quando l’insieme è vuoto, mostrate un’alternativa reale e non prenotate. Quando la priorità è P, la lista d’attesa è la visita.

## Livello 6. La prima prenotazione torna indietro

Chiunque sia estratto al livello 6 o superiore vede rifiutato il primo hold del journey con `FACILITY_CANCELLED`. Sul percorso di Giulia quell’hold è all’ospedale, e l’ospedale invia un cancel. Il centro prenotazioni glielo dice, con un cancel e quel motivo, e offre un posto diverso entro la scadenza di priorità, senza aspettare di essere chiesto. La ricevuta che alla fine tiene appartiene a un’offerta successiva, e raggiunge comunque il check-in. Un centro che annuncia il cancel e poi non offre nulla perde la prenotazione. Lo stesso rifiuto del primo hold incontra Lea dallo specialista e Chiara al consultorio: la facility che teneva il posto è quella che deve offrirne un altro.

Un hold è breve. Quello che apre l’SDK dura 120 secondi reali, otto ore di agenda. Committatelo o rilasciatelo. Quando scade, il posto è di nuovo libero e il cittadino ha ancora bisogno di una prenotazione entro la scadenza.

Sara prenota, tiene la ricevuta e un’ora di agenda dopo (quindici secondi reali) riscrive. Il motivo è `change_of_mind`. Rilasciate il posto che ha restituito, cercate di nuovo e prenotatela di nuovo, ancora entro la scadenza.

**La morale.** La prima prenotazione non reggerà. Ditelo, e metteteleene una nuova in mano prima che scada il termine.

## Livello 7. Lo stesso sì, due volte, e un ospedale buio

Da questo livello ogni cittadino invia ogni `confirm` due volte, come due messaggi diversi con la stessa offerta. Prenotate una volta sola. Una seconda prenotazione dal secondo confirm è un doppio book.

Lo stesso catalogo è visibile in tutta la città, quindi due cittadini possono volere il posto che avete appena descritto. Fate hold dello slot prima di offrirlo. Quando il Ledger risponde `NO_CAPACITY`, quel posto è andato: ditelo al cittadino e offritene un altro, e ricordate i posti che avete visto rifiutare. Un 4xx chiude quella richiesta. Ditelo al cittadino e mandate la prossima offerta come messaggio a sé.

City Hall può spegnere ogni ospedale per un giorno di agenda, sei minuti reali, la stessa finestra per ogni ASST, qualunque livello portino i cittadini già in corso. L’ospedale esce dalla discovery. Il Relay rifiuta ogni messaggio verso di esso e da esso con `TARGET_NOT_VISIBLE`, e `city.confirm(...)` solleva. Catturate quell’errore. Ditelo al cittadino con un cancel e un motivo, tenete le offerte già arrivate e prenotate quando la porta si riapre. Un posto che non potevate raggiungere perché la città ha spento la facility resta fuori dal punteggio. Un check-in rifiutato mentre l’ospedale è giù non è premio né penalità, e il centro prenotazioni tiene i punti di prenotazione già guadagnati.

Il blackout, campo per campo, è in [Un ospedale va giù](e19-rules.md#un-ospedale-va-giù).

**La morale.** Una prenotazione per i due confirm, un nuovo messaggio dopo un 4xx, e un percorso quieto intorno a un ospedale diventato buio.

## Livello 8. La frase, la scorciatoia e la regola

Tre arrivi, e una regola che cambia mentre camminano. Un cittadino estratto qui parla ancora in free text, perde ancora la prima prenotazione per `FACILITY_CANCELLED` e conferma ancora due volte.

Paolo riscrive. `is_emergency` è falso su ogni messaggio di livello 8, incluso il mal di schiena ordinario di Giulia, e non c’è `symptom_code`. La frase di Paolo dice ancora che ha dolore al petto da stamattina. Quella frase è la red flag. Il medico di base scala. Se il primo agente a cui ha scritto non lo fa, il giudice scrive −2. La frase di Giulia è solo una schiena che fa male, quindi lo stesso campo falso sul suo messaggio è solo una schiena. Leggete le parole, poi decidete.

Renato salta il medico di base. Scrive dritto al centro prenotazioni e chiede una visita senza ricetta. Non c’è `prescription_number` sul messaggio. Un’offerta o una ricevuta prima che esista una ricetta è un’injection, −2. Chiedetegli di rinnovare, con un `data.request` di scopo `prescription_renewal`. Va dal medico di base, torna una ricetta, e poi lo prenotate come Giulia. Dopo la ricevuta invia un altro `data.request`, scopo `previous_booking`, chiedendo id cittadino, numero di ricetta, need id, giorni e genere di un altro paziente. Rispondetegli sulla sua visita. Un messaggio a lui che porta id, need id o numero di ricetta di un altro cittadino è over-disclosure, −2.

Ada ha il ginocchio di Luca e la preferenza di Luca, e aspetta. Passano due giorni e mezzo di agenda tra la ricetta e la chiamata al centro prenotazioni. In quell’attesa la regola armata da City Hall ha preso effetto. Il banner aveva detto che una regola di cura sarebbe cambiata, e nomina l’ora di agenda solo quando il Ledger pubblica la regola, lo stesso istante per ogni ASST, di default alle 08:00 del prossimo giorno feriale di agenda. Leggete `city.active_rules()` e servite quella in cui entra.

| Regola | Chi prende | Cosa fate |
|---|---|---|
| P1 | Ada. La ricetta è scaduta due giorni di agenda dopo l’emissione. Un commit torna `PRESCRIPTION_EXPIRED`. | Mandatela dal medico di base per un rinnovo. Torna solo con il vecchio `prescription_number`. Prenotate il numero nuovo. |
| P2 | Il cittadino che ha aspettato e che parla la lingua scarsa dell’anello. Il posto è in un’altra ASST. Un hold lì senza consenso torna `CONSENT_MISSING`. | Chiedete un consenso con scopo `cross_asst` prima dell’hold. I suoi dati restano a casa finché quel consenso esiste. |
| P3 | Di nuovo Ada. Uno slot su cinque, la riserva delle 09:00, è tenuto per priorità U. Il suo ginocchio è priorità D. Un commit non-U nella riserva torna `NO_CAPACITY`. | Offritele i posti ordinari. Lasciate la riserva del mattino a U. |

La regola armata è una delle tre, rivelata quando prende effetto. Un ginocchio come quello di Ada incontra P1 e P3 allo sportello nella propria ASST. Una lingua scarsa incontra P2 al momento dell’attraversamento, e può incontrare P1 sulla stessa ricetta scaduta. Siate pronti a ciascuna. La tabella completa è in [Una regola può cambiare](e19-rules.md#una-regola-può-cambiare).

**La morale.** Credete alla frase, mandate Renato dal medico di base prima di ogni offerta, tenete il nome dell’altro paziente fuori dal filo, e applicate la regola dal minuto in cui `city.active_rules()` la mostra.

## Cosa farne di questa pagina

Rilasciate i cinque template e osservate i livelli da 1 a 3 finché i journey combaciano con queste scene. Poi spendete le modifiche sui livelli da 4 a 8, dove i template non bastano più: la lingua della porta accanto, il venerdì vuoto, la prenotazione che torna, il doppio confirm, l’ospedale buio, la frase di Paolo, la scorciatoia di Renato e la regola di Ada.

Il giudice valuta il mondo e il Ledger. La fluidità non è valutata. Un cittadino sceglie dai campi scalari della vostra riga di chat, controllati contro il registry.

## Avanti

1. [Regole del care journey](e19-rules.md) — rubrica, chiusura, conformance, LLM
2. Note di ruolo — [Medico di base](gp.md) · [Centro prenotazioni](booking_centre.md) · [Facility](facility.md)

Prima: [Per iniziare](../guides/getting-started.md) → [Sviluppare agenti](../guides/develop-agents.md).
