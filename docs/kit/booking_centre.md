# Booking centre

A `booking_centre` agent starts from the single agent template (`city init agent booking_centre --role booking_centre`). Your brain verifies the prescription with the Ledger (`prescription prescription_number=...`), asks consent and the citizen's constraints, offers slots from the Ledger's `world` answer inside the priority deadline (crossing to other ASSTs only when its own has none), books through the facility, and forwards the receipt. It does not close the case after the receipt: the visit is still ahead. See [templates/agent/SKILL.md](../../templates/agent/SKILL.md).
