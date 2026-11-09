# Specialista

Works as-is for the base journeys. As the hospital template, and after the check-in:

- ask consent to share the report (`market.ask_consent`, purpose `report_sharing`);
- file the report with `market.file_report` under the returned `consent_ref` (without it on a refusal), with the protocol's follow-up prescription when there is one. A refusal usually comes back as plain words: `understood()` reads it as `need.request` with no `consent_ref`, and the template files the report unshared;
- then `market.close_case`. With a follow-up the citizen goes back to a centro prenotazioni and the city ignores the close.

When `need.is_emergency` is true, call `market.escalate` and stop.
