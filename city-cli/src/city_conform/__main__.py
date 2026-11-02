"""`city-conform` runs the in-process full profile and prints the report."""

from __future__ import annotations

import asyncio

from city_conform.report import render
from city_conform.runner import run_profile
from city_conform.stages import CATALOG
from city_conform.world import good_agent


def main() -> None:
    async def _run() -> bool:
        agent = await good_agent()
        report = await run_profile(agent, CATALOG)
        print(render(report), end="")
        return report.passed

    if not asyncio.run(_run()):
        raise SystemExit(1)
