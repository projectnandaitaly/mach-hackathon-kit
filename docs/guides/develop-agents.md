# How to develop an agent

You write the **brain**. The harness owns transport, signing, lease, and ack. Prefer the full step-by-step and troubleshooting tables in the [care journey rules](../kit/e19-rules.md#how-to-develop-an-agent); this page is the short path.

## 1. Install the CLI

```bash
uv tool install city-cli
uv tool upgrade city-cli
```

Use a current `city-cli` from PyPI. Older builds may reject templates or slugs.

## 2. Log in

```bash
city login                 # or: city login --no-browser
```

Session, controller key, and LLM Gateway key land under `~/.config/city/teams/<your team>/`. Never commit that directory.

## 3. Scaffold the five agents

```bash
mkdir asst && cd asst
city init agent booking_centre    --template booking_centre
city init agent hospital          --template facility
city init agent gp                --template gp
city init agent specialist        --template specialist
city init agent family_counseling --template family_counseling
```

Templates in this kit under [`templates/`](../../templates/) match what `city init` copies. Fix the hospital card `service_types` as the rules say (`hospital.physiatry`, not the template `lab`).

## 4. Edit the brain (optional at first)

Change only `<agent>/agent/agent.py`. Import `city_sdk.market`. Speak with `text=` on the same verb that performs the structured act.

Local exercise without deploying:

```bash
city dev --chat --root family_counseling
```

## 5. Validate and deploy

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

Keep these four behaviours in `agent/agent.py` or the full suite fails the deploy. The templates already have them:

- `raise OutOfScope()` when `clinical(need.summary)` is true, and do not send or remember that text
- on `need.is_emergency`, `await market.escalate(reason="red_flag")` and return without sending
- if `need.summary == "__error__"`, raise `RuntimeError` and do not catch it
- `ask` only a peer `find_facilities` (or `rank`) returned

`city dev --chat` does not certify the agent. `city conform` without `--url` runs a built-in reference agent, not your folder. The stage names and the failure table are in [Conformance](../kit/e19-rules.md#conformance).

## LLM Gateway

`city login` downloads the team virtual key. Do not put it in `card.yaml` or the brain. Deploy injects it into the pod. See the rules section [Models (LLM Gateway)](../kit/e19-rules.md#models-llm-gateway).

## Role notes

- [Protocol / Market day 1](../kit/protocol.md)
- [Booking centre](../kit/booking_centre.md) · [Facility](../kit/facility.md) · [GP](../kit/gp.md) · [B2B](../kit/b2b.md)

## Next

- [Getting started](getting-started.md)
- [Concepts](concepts.md)
- Full rules: [e19-rules.md](../kit/e19-rules.md)
