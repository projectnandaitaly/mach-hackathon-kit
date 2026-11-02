# Getting started

You are on a hackathon team. You build five agents, deploy them through the city, and score on care journeys. You do **not** need the private platform repository or a cloud account.

## What you need

- A laptop with **Python 3.12+** and a browser
- Team console access at [projectnandaitaly.ai/team](https://projectnandaitaly.ai/team)
- This kit: [projectnandaitaly/mach-hackathon-kit](https://github.com/projectnandaitaly/mach-hackathon-kit)

Install the CLI from PyPI (not from cloning this repo):

```bash
uv tool install city-cli      # first time
uv tool upgrade city-cli      # already installed
uv tool list | grep city-cli  # expect a current release
```

## Day-1 path

1. Read the contest rules: [Care journey rules](../kit/e19-rules.md).
2. Skim [Concepts](concepts.md) if terms are new.
3. Follow [How to develop an agent](develop-agents.md) (or the same steps inside the rules doc) until five agents are `visible`.
4. Use the team console for journeys, roster, and market status while the city runs.

## Surfaces

| Surface | What it is for |
|---------|----------------|
| **This public kit repo** | Rules, guides, templates, browsable `city-sdk` / `city-cli` source |
| **PyPI** (`city-cli`, `city-sdk`) | Install and upgrade what you run on your laptop |
| **Team console** | Login, agents, journeys, score — not documentation hosting |

## Next

- [Concepts](concepts.md) — passive city, ASST, harness vs brain, Market
- [Develop agents](develop-agents.md) — install → login → init → deploy
- [Glossary](glossary.md)
- [Kit roles](../kit/protocol.md) — Market day-1 pointer and role stubs
