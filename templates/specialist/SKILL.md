# Specialist

Works as-is for the base journeys. As the hospital template, and after the check-in:

- ask consent to share the report (`market.ask_consent`, purpose `report_sharing`);
- file the report with `market.file_report` under the returned `consent_ref` (without it on a refusal), with the protocol's follow-up prescription when there is one;
- then `market.close_case`. With a follow-up the citizen goes back to a booking centre and the city ignores the close.

When `need.is_emergency` is true, call `market.escalate` and stop.
