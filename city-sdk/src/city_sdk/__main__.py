"""Container entrypoint. Without a card the harness waits; with one it serves."""

from __future__ import annotations

import asyncio
import os
import sys

from cap.core.errors import CapError
from cap.core.http_ports import Plane, listen

from city_sdk.app import create_app
from city_sdk.boot import prepare
from city_sdk.harness import Harness
from city_sdk.logs import HarnessLog
from city_sdk.utils.logging import configure, uvicorn_log_config

_STARTUP_FAILURE = 3


def main() -> None:
    try:
        ready = prepare(os.environ)
    except CapError as exc:
        raise SystemExit(exc.code) from None
    if ready is None:
        raise SystemExit("city-sdk harness waits for an agent config")
    harness, plane = ready
    console = os.environ.get("CITY_ENV") != "cloud"
    harness.log = HarnessLog(stream=sys.stdout, now=harness.started, console=console)
    configure(console=console)
    asyncio.run(_run(harness, plane, console=console))


async def _run(harness: Harness, plane: Plane, *, console: bool) -> None:
    # The Relay client pools connections on the loop that joined; /invoke must run there too.
    try:
        await harness.start()
    except CapError as exc:
        harness.log.error(
            "startup failed",
            now=harness.clock.now(),
            agent_id=harness.agent_id,
            code=exc.code,
            detail=exc.message,
            category="error",
            exc_info=True,
        )
        raise SystemExit(f"{exc.code}: {exc.message}") from None
    host, port = listen(plane)
    harness.log.info(
        "ready",
        now=harness.clock.now(),
        agent_id=harness.agent_id,
        host=host,
        port=port,
        category="lifecycle",
    )
    import uvicorn

    config = uvicorn.Config(
        create_app(harness),
        host=host,
        port=port,
        log_config=uvicorn_log_config(console=console),
    )
    server = uvicorn.Server(config)
    await server.serve()
    if not server.started:
        raise SystemExit(_STARTUP_FAILURE)


if __name__ == "__main__":
    main()
