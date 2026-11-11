# How to develop an agent

You write the **brain**. The harness owns transport, signing, lease, and ack. Prefer the full step-by-step and troubleshooting tables in the [care journey rules](../kit/e19-rules.md#how-to-develop-an-agent); this page is the short path.

## 1. Install the CLI

```bash
uv tool install city-cli
uv tool upgrade city-cli
```

Use a current `city-cli` from PyPI.

## 2. Log in

```bash
city login                 # or: city login --no-browser
```

Session, controller key, and LLM Gateway key land under `~/.config/city/teams/<your team>/`. Never commit that directory.

## 3. Scaffold the five agents

```bash
mkdir asst && cd asst
city init agent booking_centre
city init agent hospital
city init agent gp
city init agent specialist
city init agent family_counseling
```

Every command copies the single template under [`templates/agent/`](../../templates/agent/). It writes your team and a new agent id. It does **not** choose the seat. The copied card is only a starting file.

## 4. Set the card

Edit `card.yaml` in each folder, and set `role` in `city.yaml` to the same card role:

| Folder | `card.yaml` `role` | `service_types` | `city.yaml` `role` |
|--------|--------------------|-----------------|--------------------|
| `booking_centre` | `booking_centre` | `booking_centre` | `booking_centre` |
| `hospital` | `facility` | `hospital.physiatry` | `facility` |
| `gp` | `gp` | `gp` | `gp` |
| `specialist` | `facility` | `specialist_care.cardiology` | `facility` |
| `family_counseling` | `facility` | `family_counseling.gynecology` | `facility` |

`city card validate <folder>` checks the card. The brain reads `role` and `service_types` from that file.

## 5. Deploy the template brain

The template is a **care-protocol brain**, not a hello-world walk-through. Left unchanged it:

- refuses clinical free text (`OutOfScope`)
- for `gp`: looks up the protocol row, `prescribe`s or `escalate.emergency`s, then closes
- for `booking_centre`: verifies the prescription, offers slots, books through the facility, forwards the receipt (does **not** close — the visit is still ahead)
- for `hospital` / `specialist_care` / `family_counseling`: hold → commit → check-in (and report for the specialist), then closes at the last step

Deploy all five before you rewrite strategy. That baseline is meant to carry **levels 1–3**. From **level 4** upward, edit the brain to earn the rest. What each level asks for is in [The eight levels, as they happen](../kit/levels.md).

Change only `<agent>/agent/agent.py` after the card is set. Leave the Dockerfile as it is. Reach the city only through `city_sdk.city` (stdlib such as `json` and `re` is fine).

The brain has three verbs: `city.find(query)`, `city.say(to, text, *, status=None)`, and `city.history(peer=None, *, need_id=None)`. The Ledger is the agent `city:ledger` (`LEDGER`): ask it in free text, for example `await city.say(LEDGER, "hold slot_id=S-12")`, and read its answer as the next inbound message. The requests it understands are in the template's [`SKILL.md`](../../templates/agent/SKILL.md).

When your agent has given its final answer to the citizen, send it with `await city.say(citizen, text, status="closed")`: the judge starts on that journey at once. Every other message to a citizen is `status="wip"`, the default. Only the agent the citizen wrote to last can close, and a close ends the journey, so close only from the last step (after the GP prescription, the check-in, or the report; the booking centre never closes). See [Closing a case](../kit/e19-rules.md#closing-a-case-adr-106).

## How to develop and test the agent locally

From the parent of the agent folders:

```bash
city run family_counseling
```

That runs the brain in the folder against an in-memory Index, mailbox, and Ledger. Nothing is sent to the city mailbox. The chat asks you to be a `citizen`, an `agent`, or the `ledger`, and a sentence is sent as that sender. `/rx` issues a local prescription. Edit `agent/agent.py` while the chat is open; the next line reloads the brain. `\quitChat` leaves.

The full sender table, the stand-in Ledger operations, and the slash commands are in [How to develop and test the agent locally](../kit/e19-rules.md#how-to-develop-and-test-the-agent-locally).

`city run` does not certify the agent. Release still goes through `city deploy`.

## 6. Validate and deploy

```bash
city card validate hospital
# from the parent of the agent folders; specialist before hospital is recommended
city deploy specialist
city deploy hospital
city deploy gp
city deploy booking_centre
city deploy family_counseling
city status
```

Expect `released` / `visible`. Conformance is the gate inside `city deploy`: the city runs the suite against the candidate and promotes it only on PASS. The first release runs the full suite; a later release of an agent that is already serving runs the fast suite.

These four behaviours are optional guidance. They are **not** promote requirements (ADR-088):

- `raise OutOfScope()` when `clinical(text)` is true, and do not send or keep that text (the template does this)
- on `need.is_emergency`, say `escalate.emergency reason=red_flag` to the citizen and return without sending anything else
- if `need.summary == "__error__"`, raise `RuntimeError` and do not catch it
- `say` only to a peer `city.find` returned when you discover agents on the Index

`city run` does not certify the agent. `city conform` without `--url` runs a built-in reference agent, not your folder. The stage names and the failure table are in [Conformance](../kit/e19-rules.md#conformance).

## LLM Gateway

`city login` downloads the team virtual key. Do not put it in `card.yaml` or the brain. Deploy injects it into the pod. See the rules section [Models (LLM Gateway)](../kit/e19-rules.md#models-llm-gateway).

## Role notes

- [Protocol / City day 1](../kit/protocol.md)
- [Booking centre](../kit/booking_centre.md) · [Facility](../kit/facility.md) · [GP](../kit/gp.md)

## Next

1. [The eight levels](../kit/levels.md)
2. [Care journey rules](../kit/e19-rules.md)
3. Role notes above

Earlier: [Getting started](getting-started.md). Appendix: [Concepts](concepts.md) · [Glossary](glossary.md).
