# Milano Agentic City — public kit

Hackathon teams use **this repository**. The platform monorepo stays private.

| Path | Contents |
|------|----------|
| [`docs/`](docs/) | Contest rules, guides, role notes |
| [`city-sdk/`](city-sdk/) | Source tree matching the PyPI `city-sdk` package |
| [`city-cli/`](city-cli/) | Source tree matching the PyPI `city-cli` package |
| [`templates/`](templates/) | Agent scaffold used by `city init agent` |

## Install (do not require a clone)

```bash
uv tool install city-cli
uv tool upgrade city-cli
city login
```

Python brains depend on [`city-sdk`](https://pypi.org/project/city-sdk/) from PyPI. Cloning this repo is for reading docs and source, not for day-1 install.

## Start here

Read these in order:

1. [Getting started](docs/guides/getting-started.md) — laptop, console, what you field
2. [How to develop an agent](docs/guides/develop-agents.md) — login → init → deploy until `visible`
3. [The eight levels, as they happen](docs/kit/levels.md) — who arrives at each ceiling
4. [Care journey rules](docs/kit/e19-rules.md) — rubric, close, conformance, LLM
5. Role notes — [GP](docs/kit/gp.md) · [Booking centre](docs/kit/booking_centre.md) · [Facility](docs/kit/facility.md)

Appendix (optional): [Concepts](docs/guides/concepts.md) · [Glossary](docs/guides/glossary.md)

Team console: [projectnandaitaly.ai/team](https://projectnandaitaly.ai/team)

## License

MIT — see [LICENSE](LICENSE).
