"""Load and validate MHM agent cards from project ``card.yaml``."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from cap.core.errors import CapError
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from city_deploy.agent_ids import id_family

_PLACEHOLDER_X = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
_CARD_VERSION = re.compile(
    r"^(version:\s*)([\"']?)(\d+\.\d+\.\d+)\2(\s*)$",
    re.MULTILINE,
)
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
UNMINTED = "unminted"
_AGENT_LINE = re.compile(r"^agent_id:.*$", re.MULTILINE)
_MAILBOX_LINE = re.compile(r"^(\s+mailbox:\s*).*$", re.MULTILINE)
_ENDPOINTS_BLOCK = re.compile(r"^endpoints:[^\n]*\n(?:[ \t]+[^\n]*\n?)*", re.MULTILINE)


def specs_root() -> Path:
    here = Path(__file__).resolve()
    packaged = here.parent / "schemas" / "profiles" / "mhm" / "1.0" / "card.json"
    if packaged.is_file():
        return here.parent / "schemas"
    for parent in here.parents:
        candidate = parent / "specs" / "profiles" / "mhm" / "1.0" / "card.json"
        if candidate.is_file():
            return parent / "specs"
    raise CapError("SCHEMA_INVALID", "specs")


def _schema_files() -> list[Path]:
    root = specs_root()
    files: list[Path] = []
    for base in (root / "cap", root / "profiles", root / "events"):
        if base.is_dir():
            files.extend(sorted(base.rglob("*.json")))
    return files


@cache
def _registry() -> Registry:
    resources: list[tuple[str, Resource[object]]] = []
    for path in _schema_files():
        contents = json.loads(path.read_text(encoding="utf-8"))
        schema_id = contents.get("$id")
        if isinstance(schema_id, str):
            resources.append(
                (schema_id, Resource.from_contents(contents, default_specification=DRAFT202012))
            )
    return Registry().with_resources(resources)


@cache
def _mhm_validator() -> Draft202012Validator:
    schema = json.loads(
        (specs_root() / "profiles" / "mhm" / "1.0" / "card.json").read_text(encoding="utf-8")
    )
    return Draft202012Validator(
        schema,
        registry=_registry(),
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


def load_card_yaml(text: str) -> dict[str, Any]:
    loaded = yaml.safe_load(text)
    if not isinstance(loaded, dict):
        raise CapError("SCHEMA_INVALID", "card")
    return loaded


def unminted_id(raw: Mapping[str, Any]) -> str:
    """Stand-in id before the first deploy. Deploy refuses it; the CLI swaps it."""

    try:
        family = id_family(raw)
    except CapError:
        family = "agent"
    return f"agent:{UNMINTED}/{family}"


def card_from_yaml(text: str, public: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build a deployable card dict from ``card.yaml``, injecting agent keys."""
    raw = load_card_yaml(text)
    agent_id = raw.get("agent_id")
    if agent_id is None:
        agent_id = unminted_id(raw)
    team = raw.get("team")
    name = raw.get("name") or "agent"
    version = raw.get("version")
    role = raw.get("role")
    capabilities = raw.get("capabilities")
    no_clinical = raw.get("no_clinical_advice")
    if not isinstance(agent_id, str) or not isinstance(team, str) or not isinstance(version, str):
        raise CapError("SCHEMA_INVALID", "card")
    if not isinstance(role, str):
        raise CapError("SCHEMA_INVALID", "role")
    if not isinstance(capabilities, list) or not capabilities:
        raise CapError("SCHEMA_INVALID", "capabilities")
    if no_clinical is not True:
        raise CapError("SCHEMA_INVALID", "no_clinical_advice")
    if public is None:
        key = {
            "kid": f"{agent_id}#k1",
            "kty": "OKP",
            "crv": "Ed25519",
            "x": _PLACEHOLDER_X,
        }
    else:
        key = {
            "kid": f"{agent_id}#k1",
            "kty": public["kty"],
            "crv": public["crv"],
            "x": public["x"],
        }
    card: dict[str, Any] = {
        "agent_id": agent_id,
        "team": team,
        "name": name,
        "version": version,
        "cap_versions": raw.get("cap_versions") or ["1.0"],
        "profiles": raw.get("profiles") or ["mhm/1.0"],
        "keys": [key],
        "endpoints": raw.get("endpoints") or {"mailbox": agent_id},
        "role": role,
        "capabilities": capabilities,
        "no_clinical_advice": True,
    }
    for optional in ("districts", "languages", "accessibility"):
        if optional in raw:
            card[optional] = raw[optional]
    return card


def validate_mhm_card(card: Mapping[str, Any]) -> None:
    role = card.get("role")
    if role not in {"booking_centre", "facility", "gp", "b2b"}:
        raise CapError("SCHEMA_INVALID", "role")
    errors = sorted(_mhm_validator().iter_errors(dict(card)), key=lambda err: str(list(err.path)))
    if errors:
        path = ".".join(str(part) for part in errors[0].absolute_path) or "card"
        raise CapError("SCHEMA_INVALID", path)


def write_card_id(path: Path, agent_id: str) -> bool:
    """Put ``agent_id`` in ``card.yaml`` and set ``endpoints.mailbox`` to the same id.

    True when the file changed. A card with no endpoints block gains one.
    """

    text = path.read_text(encoding="utf-8")
    line = f"agent_id: {agent_id}"
    if _AGENT_LINE.search(text):
        updated = _AGENT_LINE.sub(line, text, count=1)
    else:
        updated = f"{line}\n{text}"
    if _MAILBOX_LINE.search(updated):
        updated = _MAILBOX_LINE.sub(lambda match: f"{match.group(1)}{agent_id}", updated, count=1)
    elif _ENDPOINTS_BLOCK.search(updated):
        updated = _ENDPOINTS_BLOCK.sub(
            lambda match: _with_mailbox(match.group(0), agent_id),
            updated,
            count=1,
        )
    else:
        if updated and not updated.endswith("\n"):
            updated += "\n"
        updated += f"endpoints:\n  mailbox: {agent_id}\n"
    if updated == text:
        return False
    path.write_text(updated, encoding="utf-8")
    return True


def _with_mailbox(block: str, agent_id: str) -> str:
    body = block if block.endswith("\n") else f"{block}\n"
    return f"{body}  mailbox: {agent_id}\n"


def next_patch_version(version: str) -> str:
    """Return the next patch SemVer (``1.0.0`` → ``1.0.1``)."""
    matched = _SEMVER.fullmatch(version)
    if matched is None:
        raise CapError("SCHEMA_INVALID", "version")
    major, minor, patch = matched.groups()
    return f"{major}.{minor}.{int(patch) + 1}"


def bump_card_yaml(path: Path, *, released: str) -> str | None:
    """Bump ``card.yaml`` patch after a release of ``released``.

    No-op (returns ``None``) when the file is missing, the ``version`` line is
    absent or malformed, or it no longer equals ``released`` (already bumped).
    """
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    matched = _CARD_VERSION.search(text)
    if matched is None:
        return None
    current = matched.group(3)
    if current != released:
        return None
    nxt = next_patch_version(current)

    def _replace(match: re.Match[str]) -> str:
        quote = match.group(2)
        return f"{match.group(1)}{quote}{nxt}{quote}{match.group(4)}"

    updated = _CARD_VERSION.sub(_replace, text, count=1)
    path.write_text(updated, encoding="utf-8")
    return nxt
