"""Dry-run inventory of agents and workloads left by the old lifecycle.

Prints what a later cleanup would remove. It never deletes. A written
go-ahead is required before any production removal, and this command has
no apply path.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

REMOVED_VISIBILITY = frozenset(
    {
        "registered",
        "conformance",
        "hidden",
        "quarantined",
        "suspended",
        "retired",
    }
)
_VERSIONED = re.compile(r"-\d+-\d+-\d+$")


def leftover_agents(records: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Index rows whose visibility is no longer a lifecycle state."""

    rows: list[dict[str, str]] = []
    for record in records:
        visibility = record.get("visibility")
        agent_id = record.get("agent_id")
        if not isinstance(visibility, str) or not isinstance(agent_id, str):
            continue
        if visibility in REMOVED_VISIBILITY:
            rows.append({"agent_id": agent_id, "visibility": visibility, "kind": "index"})
    rows.sort(key=lambda item: item["agent_id"])
    return rows


def leftover_workloads(deployments: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Versioned Deployment names. The serving Deployment is the agent slug."""

    rows: list[dict[str, str]] = []
    for item in deployments:
        name = item.get("name")
        app = item.get("app")
        if not isinstance(name, str) or not isinstance(app, str):
            continue
        if name != app and _VERSIONED.search(name):
            rows.append({"name": name, "app": app, "kind": "workload"})
    rows.sort(key=lambda item: item["name"])
    return rows


def inventory(
    records: Iterable[Mapping[str, Any]],
    deployments: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    agents = leftover_agents(records)
    workloads = leftover_workloads(deployments)
    return {
        "dry_run": True,
        "agents": agents,
        "workloads": workloads,
        "would_delete": len(agents) + len(workloads),
    }


def main(argv: list[str] | None = None) -> None:
    """Classify dumps. This command does not read or change production."""

    parser = argparse.ArgumentParser(
        description="Dry-run leftover lifecycle inventory. Never deletes."
    )
    parser.add_argument("--agents", type=Path, help="JSON list of {agent_id, visibility}")
    parser.add_argument("--workloads", type=Path, help="JSON list of {name, app}")
    args = parser.parse_args(argv)
    if args.agents is None and args.workloads is None:
        print(
            "dry run only: pass --agents and --workloads dumps; "
            "this command does not read production",
            file=sys.stderr,
        )
    records = _load(args.agents)
    deployments = _load(args.workloads)
    print(json.dumps(inventory(records, deployments), sort_keys=True))


def _load(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, list):
        raise SystemExit("dump must be a JSON list")
    return [item for item in loaded if isinstance(item, dict)]


if __name__ == "__main__":
    main()
