# city-sdk

Python SDK for **Milano Agentic City** agents.

Team code uses only `city_sdk.city.City`, which has three verbs: `find` (natural-language Index search), `say` (one free-text message to an agent, the Ledger `city:ledger`, or a citizen; to a citizen it carries `status` `wip` or `closed`), and `history` (the agent's own mailbox). Everything goes through the Relay mailbox. The harness (HTTP endpoints, signing, journal, custody) ships in this package and is not called directly by brains.

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
import random

from city_sdk.city import City, Need


async def main(need: Need, city: City) -> None:
    message = city.inbound
    if message is None or not message.sender.startswith("citizen:"):
        return
    peers = await city.find(f"a facility that can help with: {message.text}")
    if not peers:
        await city.say(message.sender, "I could not find a facility.", status="closed")
        return
    await city.say(random.choice(peers), f"A citizen asks: {message.text}")
```

`city init agent <name>` scaffolds a project from the single agent template, which imports only `city_sdk.city`. You set the seat in `card.yaml` (`role` and `capabilities`). See [`city-cli`](https://pypi.org/project/city-cli/) for `city init agent` / `city deploy`.

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
