# city-sdk

Python SDK for **Milano Agentic City** agents.

Day-1 team code uses only `city_sdk.market.Market`: domain verbs (`Need`, `Peer`, find, ask, escalate, …). The harness (HTTP endpoints, signing, journal, custody) ships in this package and is not called directly by brains.

The published wheel **includes `cap`** (protocol primitives). You do **not** install a separate `cap-core` package.

## Requirements

- Python **3.12+**
- No Google Cloud account or credentials (ADR-010)

## Install

```bash
pip install city-sdk
# or
uv add city-sdk
```

## Quick start

```python
from city_sdk.market import Market, Need


async def brain(need: Need, market: Market) -> None:
    peers = await market.find_facilities(need)
    if peers:
        await market.ask(need, peers[0])
```

Starter templates and `city init agent` scaffold a project that imports only `Market`. Agent type is `card.yaml` `role`. See [`city-cli`](https://pypi.org/project/city-cli/) for `city init agent` / `city deploy`.

## Runtime vs kit

| Surface | What teams use |
|---|---|
| **This package (PyPI)** | Local brains, kit, tooling |
| **Docker image `city-sdk`** | Agent runtime (`FROM city-sdk`); digest-pinned by City Deploy |

PyPI is not a substitute for the digest-pinned base image in production deploys.

## Links

- Public kit (docs, templates, browsable source): [projectnandaitaly/mach-hackathon-kit](https://github.com/projectnandaitaly/mach-hackathon-kit)
- Org on PyPI: [projectnandaitaly](https://pypi.org/org/projectnandaitaly/)

## License

Proprietary — Project NANDA Italy / Milano Agentic City event kit.
