# Medico di base

Works as-is for the base journeys. On a `need.request` from a citizen:

- a red flag (`is_emergency`, or a protocol red flag in the text): `city.escalate`, then `city.close_case`;
- a `symptom_code`, or a keyword of a protocol row in the text: `city.prescribe` the procedure and priority of that row, then `city.close_case` (ignored by the city when the citizen has moved on to the centro prenotazioni);
- only a `prescription_number`: renew it with the same procedure and priority when the Ledger says it expired (rule P1).

The medico di base never reasons clinically: everything comes from the protocol table in `city.world()`.
