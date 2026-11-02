"""Process selectors for the control-plane and worker images."""

from __future__ import annotations

import os
import runpy

CONTROL = {
    "relay": "relay",
    "index": "city_index",
    "attestation": "city_attestation",
    "deploy": "city_deploy.serve",
    "console": "city_console",
}

WORKERS = {
    "prober": "city_prober",
    "demand": "city_demand",
    "sealer": "city_attestation.seal",
}


def run_control() -> None:
    _run(os.environ.get("CITY_SERVICE", ""), CONTROL, "CITY_SERVICE")


def run_worker() -> None:
    _run(os.environ.get("CITY_WORKER", ""), WORKERS, "CITY_WORKER")


def _run(name: str, modules: dict[str, str], label: str) -> None:
    module = modules.get(name)
    if module is None:
        raise SystemExit(label)
    runpy.run_module(module, run_name="__main__")
