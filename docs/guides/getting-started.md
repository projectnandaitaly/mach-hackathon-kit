# Getting started

You are on a hackathon team. You field **five agents**, deploy them through the city, and score on care journeys. You do **not** need the private platform repository or a cloud account.

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

## What you field

One agent per role, all from the same scaffold (`city init agent … --role …`):

| Role | `--role` |
|------|----------|
| Centro prenotazioni | `booking_centre` |
| Ospedale | `hospital` |
| Medico di base | `gp` |
| Specialista | `specialist_care` |
| Consultorio | `family_counseling` |

Deploy the template as it is first. It already runs a care journey and is meant to carry **levels 1–3**. From level 4 onward you change the brain to earn more.

## Surfaces

| Surface | What it is for |
|---------|----------------|
| **This public kit repo** | Rules, guides, template, browsable `city-sdk` / `city-cli` source |
| **PyPI** (`city-cli`, `city-sdk`) | Install and upgrade what you run on your laptop |
| **Team console** | Login, agents, journeys, score — not documentation hosting |

## Next

Continue in order:

1. [How to develop an agent](develop-agents.md) — login → init → deploy until `visible`
2. [The eight levels](../kit/levels.md)
3. [Care journey rules](../kit/e19-rules.md)
4. Role notes — [GP](../kit/gp.md) · [Booking centre](../kit/booking_centre.md) · [Facility](../kit/facility.md)

Appendix: [Concepts](concepts.md) · [Glossary](glossary.md)
