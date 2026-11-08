# Centro prenotazioni

Works as-is for the base journeys. Per journey, state is kept with `market.remember` under `need:<need_id>`.

1. `need.request`: verify the prescription; a missing, foreign or expired one sends the citizen back to the medico di base (`request_data`, purpose `prescription_renewal`).
2. Ask booking consent, then the citizen's available days and gender preference.
3. Offer up to three own slots inside the priority deadline (priority P gets a waiting-list offer). With none, ask facilities of other ASSTs with `request_offers`; with none either, offer relaxed alternatives.
4. `confirm`: forward it to the facility (asking `cross_asst` consent first under rule P2), then forward the receipt.
5. A facility `cancel`: tell the citizen and offer again without that slot.

It never sends `case.closed`: after the receipt the citizen still has the visit ahead.
