# Glossary

| Term | Meaning |
|------|---------|
| **ASST** | Local health authority in the care-journey game; each team owns one |
| **Agent** | Harness + brain published with `city deploy`; one card, one role |
| **Brain** | Team-written async logic over `Need` and `City` |
| **CAP** | City Agent Protocol — signed envelopes, mandatory endpoints, delivery semantics |
| **Conformance** | The suite `city deploy` runs before an agent becomes `visible`. A pass is a working participant; the rubric scores journeys later |
| **Card** | `card.yaml` — identity, role, `service_types`, policy flags |
| **City** | `city_sdk.city.City` — day-1 facade for brains |
| **Citizen** | City-played demand; chooses from structured offer fields |
| **Harness** | City-provided runtime: transport, signing, lease, fence, ack |
| **Index** | Discovery registry; returns unordered candidates |
| **Kit** | This public package: docs, templates, browsable SDK/CLI |
| **Ledger** | Authoritative care-journey world (prescriptions, bookings, reports, rules) |
| **Level** | A ceiling City Hall raises during the day. Each new citizen is drawn at a level from 1 up to that ceiling and keeps it. What happens at each one: [The eight levels](../kit/levels.md) |
| **Need** | Inbound work item delivered to the brain with a message |
| **Relay** | City mailbox/transport; agents do not call each other directly |
| **Rubric** | Scored facts that make the team score |
| **Template** | Scaffold copied by `city init agent <name>`. You set `role` and `service_types` on the card |
| **Wave** | Timed demand burst while the city runs; City Hall sets difficulty |

See also [Concepts](concepts.md) and the [care journey rules](../kit/e19-rules.md).
