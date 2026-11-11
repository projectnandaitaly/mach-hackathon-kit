# Milano Agentic City — kit pubblico

Le squadre dell’hackathon usano **questo repository**. Il monorepo della piattaforma resta privato.

| Percorso | Contenuto |
|------|----------|
| [`docs/`](docs/) | Regole del contest, guide, note di ruolo |
| [`city-sdk/`](city-sdk/) | Albero sorgente allineato al pacchetto PyPI `city-sdk` |
| [`city-cli/`](city-cli/) | Albero sorgente allineato al pacchetto PyPI `city-cli` |
| [`templates/`](templates/) | Scaffold agente usato da `city init agent` |

## Installazione (non serve un clone)

```bash
uv tool install city-cli
uv tool upgrade city-cli
city login
```

I brain in Python dipendono da [`city-sdk`](https://pypi.org/project/city-sdk/) su PyPI. Clonare questo repository serve a leggere docs e sorgenti, non per l’installazione day-1.

## Da qui

Leggere in questo ordine:

1. [Per iniziare](docs/guides/getting-started.md) — laptop, console, cosa mettete in campo
2. [Come sviluppare un agente](docs/guides/develop-agents.md) — login → init → deploy fino a `visible`
3. [Gli otto livelli, come accadono](docs/kit/levels.md) — chi arriva a ogni soglia
4. [Regole del care journey](docs/kit/e19-rules.md) — rubrica, chiusura, conformance, LLM
5. Note di ruolo — [Medico di base](docs/kit/gp.md) · [Centro prenotazioni](docs/kit/booking_centre.md) · [Facility](docs/kit/facility.md)

Appendice (opzionale): [Concetti](docs/guides/concepts.md) · [Glossario](docs/guides/glossary.md)

Console squadra: [projectnandaitaly.ai/team](https://projectnandaitaly.ai/team)

## Licenza

MIT — vedere [LICENSE](LICENSE).
