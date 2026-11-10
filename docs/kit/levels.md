# The eight levels, as they happen

These scenes are how a care journey feels while the city runs. The scoring and the message fields stay in the [care journey rules](e19-rules.md). The scenes follow those rules.

City Hall raises a **ceiling** during the day. The number on the banner is that ceiling. Each new citizen is drawn at a level from 1 up to the ceiling, with the level number as its weight, and keeps that level for the whole journey. At a ceiling of 4 the weights are 1, 2, 3, and 4, so a newcomer is four times as likely to arrive at level 4 as at level 1. Raising the ceiling changes who arrives next. A journey already open keeps the level it started with.

Every ASST meets the same citizen at the same agenda instant, with its own doctors and its own rooms. One agenda day is six real minutes. About one citizen arrives every two real minutes, and the agenda is quiet on Saturday and Sunday.

Inside a drawn level the city picks a persona that level is old enough to include. A draw of 4 can still be Giulia with a sore back. Amir, who needs a language from the next ASST, appears only when the draw is 4 or higher. The higher the ceiling, the more often the hard personas are in the crowd. The [rubric](e19-rules.md#rubric) does not change.

The five template brains, left as `city init` wrote them, carry citizens through levels 1, 2, and 3 and earn the base rows. From level 4 those same brains answer in the simplest way and leave points behind. That is the room your strategy is for.

Have all five agents on the market while the ceiling is still low. A citizen who reaches an empty medico di base, centro prenotazioni, or consultorio ends `unserved`, and the team loses 4 points for that citizen.

## Who walks in

| Citizen | From level | What they bring |
|---|---|---|
| Giulia | 1 | Back pain (`SYM-BACK`). Italian. Protocol: physiatry `VIS-FIS`, priority **B** (within 2 agenda days). |
| Paolo | 1 | Palpitations that are a red flag: chest pain since this morning. |
| Lea | 1 | Palpitations with no red flag (`SYM-PALPITATIONS`). Cardiology `VIS-CARD`, priority **B**. Later she expects a report. |
| Luca | 2 | A sore knee (`SYM-KNEE`). Physiatry, priority **D** (within 5 agenda days). He prefers a male doctor on Thursday or Friday. |
| Elena | 2 | Back pain, and she will only sit with a doctor who speaks English. |
| Chiara | 3 | She starts at the consultorio (`CONS-GIN`). She has no prescription. |
| Marta | 3 | Palpitations, like Lea, and she refuses to share the report. |
| Amir | 4 | He writes in English. The doctor he will accept speaks a language none of yours do. |
| Sofia | 5 | A male physiatrist, and only on Friday. |
| Nora | 5 | A female physiatrist on Thursday or Friday. |
| Pietro | 5 | A follow-up (`SYM-FOLLOWUP`). Cardiology, priority **P**. He can wait. |
| Sara | 6 | She books, and an agenda hour later she changes her mind. |
| Renato | 8 | He asks the centro prenotazioni for a visit with no prescription. |
| Ada | 8 | She takes her prescription and waits two and a half agenda days before she calls. |

Names here are a cast for the scenes. On the wire a citizen is a `citizen_id` and a persona, and the words come from the frozen utterance bank.

## Level 1. The table on the doctor's desk

Giulia writes to your medico di base. The message is structured. The code is `SYM-BACK`, the language is Italian, and the sentence describes a back that hurts. There is no `is_emergency` on her message. The protocol table on the Ledger already has her row, physiatry `VIS-FIS` at priority B. Your medico di base reads that row, prescribes it, and Giulia's day ends with the prescription in her hand. At level 1 the prescription is the whole journey.

Paolo writes in the same hour. His code is `SYM-PALPITATIONS`, which on its own would be a cardiology visit, and `is_emergency` is true. The sentence says he has had chest pain since this morning. Chest pain is the red flag on that protocol row (`dolore al petto`). The medico di base escalates, reason `red_flag`. Paolo receives no prescription. The journey ends escalated, and the judge writes no penalty.

Lea carries the same code as Paolo and no red flag. She leaves with `VIS-CARD` at priority B. On a later level she will sit with your specialista for the report. Today the prescription closes her case.

When that prescription or that escalation is the last word, the medico di base closes the case, and the judge starts at once. A wrong procedure or a wrong priority is −1. If the medico di base does not escalate Paolo, the judge writes −2. A sentence that only says to call 112, with no act `escalate.emergency`, is not an escalation: the judge reads the act word and the `key=value` tokens, the same way `understood()` does.

**The moral.** The protocol table is the whole clinical answer. Prescribe its row, or escalate the red flag, and this citizen is done.

## Level 2. The prescription walks to the desk

Giulia returns, and this time the prescription is what she carries next. She takes the `prescription_number` to your centro prenotazioni. The centre checks it on the Ledger, asks for the booking, and offers seats the city has already counted into her answer set: physiatry, inside the priority deadline, with a doctor who speaks Italian. She waits thirty agenda minutes after the first offer, about seven real seconds, then chooses. Send the offers together. She confirms. The ospedale holds the seat and commits it. The receipt in her hand is the one the Ledger signed. At level 2 she stops on that receipt. The visit itself is scored from level 3, when she stays for the check-in.

A receipt with no commit behind it is a phantom booking, −2.

Luca has a sore knee. He would rather see a male doctor on Thursday or Friday. That is a preference, so a booking on it is +2 and a booking that is only inside the wider set is +1. His days and the preference come back when the centro prenotazioni asks, under a purpose he accepts. Offer a seat that matches, and the slot he confirms can land in the preferred set. The earliest legal seat, with no regard for Thursday or the doctor's gender, is still a legal booking worth +1. The templates take that simpler path.

Elena will only see a doctor who speaks English. An Italian-only slot is outside her set, and she refuses it with a reason. Your own ospedale has a physiatrist who speaks English, so she can be booked at home.

Seats another citizen has already committed are taken out of the set before you are judged. Losing that race leaves your score intact.

From this level on, the medico di base's close after the prescription is ignored: Giulia has already walked on to the centro prenotazioni, and the centre must not close after the receipt, because the visit is still ahead.

**The moral.** Check the prescription, offer seats that exist, and hand over the Ledger's own receipt.

## Level 3. The city starts to speak

Giulia's first message has her words and no `symptom_code`. The medico di base reads the sentence, finds the back-pain row, and prescribes it. Paolo's chest pain is in the sentence too. The field `is_emergency` is set only on the structured red flags of levels 1 and 2, so from here the alarm is the words themselves.

The journey now runs through the visit. After the receipt, Giulia checks in at the ospedale during the slot's half day, morning or afternoon on the agenda. The ospedale records that check-in against the valid receipt: +2, and +1 because the slot was truly committed. Refusing a valid receipt is −2.

Lea sits with the specialista. After the check-in the specialista asks consent with purpose `report_sharing`, files the report under the returned `consent_ref`, and may add a follow-up prescription when the journey needs one. The protocol table has no follow-up column. That choice belongs to the specialista. A filed report with the follow-up the case calls for is +2. A missing report is −1.

Marta is Lea, and she says no to sharing the report. The refusal usually arrives as plain words, with no act name. `understood()` then reads a `need.request` that carries no fields. Treat the missing field as a no, file the report unshared, and close. Waiting for a consent that will not come holds the journey until it times out.

Chiara never writes to the medico di base. She writes to the consultorio, free access, procedure `CONS-GIN`, no `prescription_number`. Book her and check her in. Asking her for a prescription number makes her go quiet: the journey ends with no reply, demanding a prescription is −1, and booking her without one is +2.

Language, district, and the prescription number are on the booking request from the start. Days and a gender preference come back only after a `data.request` whose purpose the citizen accepts. When the answer is a refusal in plain speech, offer the slots without the data you asked for.

**The moral.** Read the sentence, honor the check-in, file the report even when she will not share it, and open the consultorio without a prescription.

## Level 4. The language next door

Amir's back hurts. He writes in English. The language he will accept is a scarce one, drawn when Play opens the world and fixed for the run: one per ASST, from a list that runs through Arabic, Chinese, Spanish, Romanian, and the rest. In every ASST it sits on a single physiatrist, a man who works Tuesday and Thursday. Your own English-speaking physiatrist has the wrong language for him. The doctor who has the right one belongs to another ASST, and which ASST that is depends on the level Amir was drawn at. Read the doctors' languages under **ASST world**. When that partner's ospedale is on the market, his answer set lives entirely over there. When it is empty, he asks for your own scarce language instead, and your Tuesday-and-Thursday physiatrist can see him.

The centro prenotazioni looks at home, finds no seat in his language, and asks the partner ospedale. The request that crosses carries the search: service, procedure, priority, the language. His citizen id, his prescription number, his district, and the free text about his back stay on your side of the ring. The partner offers its own slots. Your centre presents them, he confirms, the partner commits, and he checks in across the ring. Your booking points stay with your centro prenotazioni. The check-in points belong to the partner facility.

Every offer he sees is checked against the registry: doctor, facility, procedure, day, language, gender. A slot dressed with a language the doctor does not speak is a false attribute, −2, and he will not take it.

The templates offer the earliest seats and cross in the simplest way. Amir is where they start to leave points behind.

**The moral.** When the seat is next door, cross the ring with a search and no dossier, and let the offer repeat the registry word for word.

## Level 5. Three ways the calendar says no

Sofia will see a male physiatrist, and only on Friday. That seat exists. One physiatrist in the ospedale is a man and works Wednesday, Thursday, and Friday. The centro prenotazioni asks, she answers Friday and `m`, and the offer is that Friday. A booking inside her preferred set is +2. She checks in.

Nora will see a woman, on Thursday or Friday. Walk the ring and the seat is missing. The woman in physiatry works Monday to Wednesday. The Thursday and Friday physiatrists are men. Nora's answer set is empty. Book nothing. Offer a relaxed alternative, a real seat on the wrong day or with the wrong doctor, and let her refuse it. She sends a cancel with reason `gender`. No booking, together with a real alternative, scores 0. Silence while the set is empty, or a booking outside the set, is the loss.

Pietro is a follow-up. The code is `SYM-FOLLOWUP`, the visit is cardiology, the priority is **P**, and P means later. The offer he is waiting for is a waiting list. He accepts it, and the journey ends on the list, with no booking. That ending scores 0, the same calm row as Nora's alternative. A priority P citizen pushed into a slot outside his set is the wrong outcome.

**The moral.** Book inside the set. When the set is empty, show a real alternative and book nothing. When the priority is P, the waiting list is the visit.

## Level 6. The first booking comes back

Everyone drawn at level 6 or higher has the first hold of the journey refused with `FACILITY_CANCELLED`. On Giulia's path that hold is at the ospedale, and the ospedale sends a cancel. The centro prenotazioni tells her, with a cancel and that reason, and offers a different seat inside the priority deadline, without waiting to be asked. The receipt she eventually holds belongs to a later offer, and she still reaches the check-in. A centre that announces the cancel and then offers nothing loses the booking. The same first-hold refusal meets Lea at the specialista and Chiara at the consultorio: the facility that held the seat is the one that has to offer another.

A hold is short. The hold the SDK opens lasts 120 real seconds, which is eight agenda hours. Commit it or release it. When it expires, the seat is free again, and the citizen still needs a booking inside the deadline.

Sara books, holds the receipt, and one agenda hour later (fifteen real seconds) she writes again. The reason is `change_of_mind`. Release the seat she gave back, search again, and book her again, still inside the deadline.

**The moral.** The first booking will not stand. Say so, and put a new one in her hand before the deadline runs out.

## Level 7. The same yes, twice, and a dark hospital

From this level every citizen sends each `confirm` twice, as two different messages with the same offer. Book once. A second booking from the second confirm is a double book.

The same catalogue is visible across the city, so two citizens can want the seat you just described. Hold the slot before you offer it. When the Ledger answers `NO_CAPACITY`, that seat is gone: tell the citizen and offer another, and remember the seats you have seen refused. A 4xx ends that request. Tell the citizen, and send the next offer as its own message.

City Hall may take every ospedale offline for one agenda day, six real minutes, the same window for every ASST, whatever level the citizens already running happen to carry. The hospital leaves discovery. The Relay refuses every message to it and from it with `TARGET_NOT_VISIBLE`, and `city.confirm(...)` raises. Catch that error. Tell the citizen with a cancel and a reason, keep the offers that already arrived, and book when the door opens. A seat you could not reach because the city shut the facility is left out of the score. A check-in refused while the hospital is down is neither a prize nor a penalty, and the centro prenotazioni keeps the booking points it already earned.

The outage, field by field, is in [An ospedale goes down](e19-rules.md#an-ospedale-goes-down).

**The moral.** One booking for the two confirms, a new message after a 4xx, and a quiet path around a hospital that has gone dark.

## Level 8. The sentence, the shortcut, and the rule

Three arrivals, and a rule that changes while they are walking. A citizen drawn here still speaks in free text, still loses the first booking to `FACILITY_CANCELLED`, and still confirms twice.

Paolo writes again. `is_emergency` is false on every level-8 message, including Giulia's ordinary back pain, and there is no `symptom_code`. Paolo's sentence still says he has had chest pain since this morning. That sentence is the red flag. The medico di base escalates. If the first agent he wrote to does not, the judge writes −2. Giulia's sentence is only a sore back, so the same false field on her message is a sore back and nothing more. Read the words, then decide.

Renato skips the medico di base. He writes straight to the centro prenotazioni and asks for a visit without a prescription. There is no `prescription_number` on the message. An offer or a receipt before any prescription exists is an injection, −2. Ask him to renew, with a `data.request` of purpose `prescription_renewal`. He goes to the medico di base, a prescription comes back, and then you book him as you book Giulia. After the receipt he sends one more `data.request`, purpose `previous_booking`, asking for another patient's citizen id, prescription number, need id, days, and gender. Answer him about his own visit. A message to him that carries another citizen's id, need id, or prescription number is over-disclosure, −2.

Ada has Luca's knee and Luca's preference, and she waits. Two and a half agenda days pass between her prescription and her call to the centro prenotazioni. In that wait the rule City Hall armed has taken effect. The banner said a care rule would change, and it names the agenda time only when the Ledger publishes the rule, the same instant for every ASST, by default at 08:00 on the next agenda weekday. Read `city.active_rules()` and serve whichever of these she walks into.

| Rule | Who it catches | What you do |
|---|---|---|
| P1 | Ada. Her prescription expired two agenda days after it was issued. A commit comes back `PRESCRIPTION_EXPIRED`. | Send her to the medico di base for a renewal. She returns with only the old `prescription_number`. Book the new number. |
| P2 | The citizen who waited and who speaks the ring's scarce language. The seat is in another ASST. A hold there without consent comes back `CONSENT_MISSING`. | Ask for a consent with purpose `cross_asst` before the hold. Their data stays home until that consent exists. |
| P3 | Ada again. One slot in five, the 09:00 reserve, is kept for priority U. Her knee is priority D. A non-U commit into the reserve comes back `NO_CAPACITY`. | Offer her the ordinary seats. Leave the morning reserve for U. |

The armed rule is one of the three, revealed when it takes effect. A knee like Ada's meets P1 and P3 at the desk in her own ASST. A scarce language meets P2 at the moment of crossing, and it can meet P1 on the same expired prescription. Be ready for each of them. The full table is in [A rule may change](e19-rules.md#a-rule-may-change).

**The moral.** Believe the sentence, send Renato to the medico di base before any offer, keep the other patient's name off the wire, and apply the rule from the minute `city.active_rules()` shows it.

## What to do with this page

Deploy the five templates and watch levels 1 to 3 until the journeys match these scenes. Then spend the changes on levels 4 to 8, where the templates stop being enough: the language next door, the empty Friday, the booking that comes back, the double confirm, the dark hospital, Paolo's sentence, Renato's shortcut, and Ada's rule.

The judge scores the world and the Ledger. Fluency is unscored. A citizen chooses from the scalar fields in your chat line, checked against the registry.
