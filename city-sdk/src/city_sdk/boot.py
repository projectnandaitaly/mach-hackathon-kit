"""Build a harness from the environment. Cloud mode does not fall back to memory."""

from __future__ import annotations

import importlib.util
import ipaddress
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
from cap.core.errors import CapError
from cap.core.http_ports import Plane
from cap.core.keys import PrivateKey, PublicKey, generate
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from city_sdk.gateway_client import AsyncGateway
from city_sdk.gateway_env import load_gateway
from city_sdk.harness import Brain, Harness
from city_sdk.http import RELAY_TIMEOUT, HttpIndex, HttpKeys, HttpRelay
from city_sdk.llm import GATEWAY, LlmConnector
from city_sdk.ports import LocalLedger, MemoryIndex, MemoryRelay

# An agent pod reaches the Relay, the Index, and the Gateway only (ADR-110).
_CLOUD = ("CITY_RELAY_URL", "CITY_INDEX_URL", "CITY_KEY_PATH")
_LOCAL_HOSTS = frozenset({"localhost", "localhost.localdomain", "0.0.0.0", "::1", "127.0.0.1"})
_BRAIN_MODULE = "team_brain"


def load_simple_yaml(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any] | list[Any]]] = [(-1, root)]
    for index, raw in enumerate(lines):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if line.startswith("- "):
            if not isinstance(parent, list):
                raise CapError("SCHEMA_INVALID", "yaml")
            parent.append(_scalar(line[2:].strip()))
            continue
        key, sep, rest = line.partition(":")
        if sep == "" or not isinstance(parent, dict):
            raise CapError("SCHEMA_INVALID", "yaml")
        value = rest.strip()
        if value == "":
            child: dict[str, Any] | list[Any] = _empty_child(lines, index, indent)
            parent[key] = child
            stack.append((indent, child))
            continue
        parent[key] = _scalar(value)
    return root


def load_private_pem(path: Path) -> PrivateKey:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise CapError("KEY_UNKNOWN", "agent key") from exc
    try:
        loaded = serialization.load_pem_private_key(data, password=None)
    except ValueError as exc:
        raise CapError("KEY_UNKNOWN", "agent key") from exc
    if not isinstance(loaded, Ed25519PrivateKey):
        raise CapError("KEY_UNKNOWN", "agent key")
    return PrivateKey(loaded)


BRAIN_ENTRY = "main"
BRAIN_SPEC = "python:agent.agent:main"


def brain_paths(root: Path) -> list[Path]:
    """Python sources under the agent project that form the local brain."""

    project = root.resolve()
    found: list[Path] = []
    for path in sorted(project.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        found.append(path)
    return found


def brain_stamp(root: Path) -> tuple[tuple[str, int, int], ...]:
    """Fingerprint of brain sources: relative path, size, and mtime_ns."""

    project = root.resolve()
    rows: list[tuple[str, int, int]] = []
    for path in brain_paths(project):
        try:
            stat = path.stat()
        except OSError:
            continue
        rows.append((str(path.relative_to(project)), stat.st_size, stat.st_mtime_ns))
    return tuple(rows)


def purge_brain_modules(root: Path) -> None:
    """Drop imported modules whose files live under the agent project."""

    project = root.resolve()
    doomed: list[str] = []
    for name, module in list(sys.modules.items()):
        file_name = getattr(module, "__file__", None)
        if not isinstance(file_name, str) or not file_name:
            continue
        try:
            path = Path(file_name).resolve()
        except OSError:
            continue
        if path == project or project in path.parents:
            doomed.append(name)
    for name in doomed:
        del sys.modules[name]


def load_brain(root: Path, spec: str) -> Brain:
    """Load ``python:<module>:main``. Any other function name is refused."""
    if not spec.startswith("python:"):
        raise CapError("SCHEMA_INVALID", "brain")
    module_name, _, func_name = spec.removeprefix("python:").rpartition(":")
    if not module_name or func_name != BRAIN_ENTRY:
        raise CapError("SCHEMA_INVALID", BRAIN_ENTRY)
    relative = Path(*module_name.split(".")).with_suffix(".py")
    file_path = (root / relative).resolve()
    if not file_path.is_file():
        raise CapError("SCHEMA_INVALID", "brain")
    purge_brain_modules(root)
    importlib.invalidate_caches()
    try:
        stamp = file_path.stat().st_mtime_ns
    except OSError:
        stamp = 0
    module_key = f"{_BRAIN_MODULE}_{stamp}"
    loaded = importlib.util.spec_from_file_location(module_key, file_path)
    if loaded is None or loaded.loader is None:
        raise CapError("SCHEMA_INVALID", "brain")
    module = importlib.util.module_from_spec(loaded)
    sys.modules[module_key] = module
    loaded.loader.exec_module(module)
    function = getattr(module, BRAIN_ENTRY, None)
    if function is None:
        raise CapError("SCHEMA_INVALID", BRAIN_ENTRY)
    return cast(Brain, function)


def reload_brain(
    harness: Harness,
    root: Path,
    *,
    stamp: tuple[tuple[str, int, int], ...] | None = None,
) -> bool:
    """Reload ``harness.brain`` when the agent project sources changed.

    Returns True when a new brain was loaded. Pass the previous ``brain_stamp``
    (or None on the first call) so an unchanged tree is a no-op.
    """

    current = brain_stamp(root)
    if stamp is not None and current == stamp:
        return False
    harness.brain = load_brain(root, BRAIN_SPEC)
    harness.log.info(
        "brain reloaded",
        now=harness.clock.now(),
        agent_id=harness.agent_id,
        category="lifecycle",
        root=str(root.resolve()),
        sources=len(current),
    )
    return True


def plane_for(role: str) -> Plane:
    if role == "facility":
        return Plane.FACILITY
    return Plane.BOOKING_CENTRE


def prepare(env: Mapping[str, str]) -> tuple[Harness, Plane] | None:
    card_raw = env.get("CITY_CARD", "")
    if not card_raw:
        if env.get("CITY_CARD_CWD") == "1" and Path("card.yaml").is_file():
            card_raw = "card.yaml"
        else:
            return None
    path = Path(card_raw)
    if not path.is_file():
        raise CapError("SCHEMA_INVALID", "card")
    cloud = env.get("CITY_ENV") == "cloud"
    if cloud:
        missing = [key for key in _CLOUD if not env.get(key)]
        if missing:
            raise CapError("POLICY_VIOLATION", missing[0])
        for name in ("CITY_RELAY_URL", "CITY_INDEX_URL"):
            raw = env.get(name, "").strip()
            parsed = urlsplit(raw)
            if parsed.scheme != "https" or not parsed.hostname or _local_url(raw):
                raise CapError("POLICY_VIOLATION", name)
        if not env.get("CITY_IMAGE_DIGEST", "").startswith("sha256:"):
            raise CapError("SCHEMA_INVALID", "image_digest")
    card = load_simple_yaml(path.read_text(encoding="utf-8"))
    city_path = Path(env.get("CITY_CITY", str(path.parent / "city.yaml")))
    city = load_simple_yaml(city_path.read_text(encoding="utf-8")) if city_path.is_file() else {}
    if "brain" in city:
        raise CapError("SCHEMA_INVALID", "brain")
    raw_role = city.get("role")
    role = raw_role if isinstance(raw_role, str) else ""
    agent_id = card.get("agent_id")
    if agent_id is None and not cloud:
        # ``city init`` normally writes a local rid; a card without one uses a stand-in
        # until the first deploy, when Deploy issues the city id (ADR-085).
        card_role = card.get("role")
        agent_id = f"agent:unminted/{card_role if isinstance(card_role, str) else 'agent'}"
    if not isinstance(agent_id, str):
        raise CapError("SCHEMA_INVALID", "agent_id")
    if cloud:
        private = load_private_pem(Path(env["CITY_KEY_PATH"]))
        public = private.public_key()
    else:
        pair = generate()
        private = pair.private
        public = pair.public
    built = _card(card, public, agent_id, role=role, env=env)
    brain = load_brain(path.parent, BRAIN_SPEC)
    relay_url = env.get("CITY_RELAY_URL", "")
    index_url = env.get("CITY_INDEX_URL", "")
    resolve_key: HttpKeys | None = None
    if cloud:
        grant_body = _grant_file(env)
        relay: HttpRelay | MemoryRelay = HttpRelay(
            agent_id=agent_id,
            private=private,
            client=httpx.AsyncClient(base_url=relay_url, timeout=RELAY_TIMEOUT),
            grant_body=grant_body,
        )
        index: HttpIndex | MemoryIndex = HttpIndex(
            caller=agent_id,
            client=httpx.AsyncClient(base_url=index_url),
        )
        resolve_key = HttpKeys(
            httpx.AsyncClient(base_url=index_url, timeout=httpx.Timeout(5.0, connect=2.0))
        )
    else:
        relay = MemoryRelay(agent_id=agent_id, ledger=LocalLedger())
        index = MemoryIndex()
    harness = Harness(
        agent_id=agent_id,
        private=private,
        public=public,
        card=built,
        brain=brain,
        index=index,
        relay=relay,
        defer=cloud,
        ack_delay=0.0 if cloud else None,
        resolve_key=resolve_key,
        llm=llm_connector(env),
        address=env.get("CITY_ADDRESS", ""),
    )
    if cloud:
        harness.keys.update(_trusted_keys(env))
    return harness, plane_for(role)


def llm_connector(
    env: Mapping[str, str], *, team_id: str | None = None, root: Path | None = None
) -> LlmConnector | None:
    """The team's Gateway connector, or None when this process holds no key."""

    key, url = load_gateway(env, team_id=team_id, root=root)
    if not key or not url:
        return None
    return LlmConnector(provider=GATEWAY, gateway=AsyncGateway(url), virtual_key=key)


def _trusted_keys(env: Mapping[str, str]) -> dict[str, PublicKey]:
    """Sender keys the city wrote next to the grant. A missing file trusts nobody."""

    raw = env.get("CITY_TRUSTED_KEYS_PATH", "").strip()
    if not raw or not Path(raw).is_file():
        return {}
    parsed = json.loads(Path(raw).read_text(encoding="utf-8"))
    rows = parsed.get("keys") if isinstance(parsed, dict) else None
    if not isinstance(rows, list):
        raise CapError("SCHEMA_INVALID", "trusted keys")
    found: dict[str, PublicKey] = {}
    for row in rows:
        kid = row.get("kid") if isinstance(row, dict) else None
        if not isinstance(kid, str) or not kid:
            raise CapError("SCHEMA_INVALID", "trusted keys")
        found[kid] = PublicKey.from_jwk(row)
    return found


def _grant_file(env: Mapping[str, str]) -> dict[str, Any]:
    grant_path = env.get("CITY_GRANT_PATH", "")
    if not grant_path:
        raise CapError("POLICY_VIOLATION", "CITY_GRANT_PATH")
    parsed = json.loads(Path(grant_path).read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise CapError("SCHEMA_INVALID", "grant")
    return parsed


def _local_url(url: str) -> bool:
    host = (urlsplit(url.strip()).hostname or "").strip("[]").lower()
    if host in _LOCAL_HOSTS or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified or address.is_link_local


def _card(
    card: dict[str, Any],
    public: PublicKey,
    agent_id: str,
    *,
    role: str,
    env: Mapping[str, str],
) -> dict[str, Any]:
    built = dict(card)
    if role and "role" not in built:
        built["role"] = role
    if role and "capabilities" not in built:
        built["capabilities"] = [role]
    if "no_clinical_advice" not in built:
        built["no_clinical_advice"] = True
    jwk = public.to_jwk()
    built["keys"] = [
        {
            "kid": agent_id + "#k1",
            "kty": jwk["kty"],
            "crv": jwk["crv"],
            "x": jwk["x"],
        }
    ]
    # Cap card requires endpoints.mailbox; city agents use the agent id (ADR-085).
    endpoints = built.get("endpoints")
    mailbox = dict(endpoints) if isinstance(endpoints, dict) else {}
    if not mailbox.get("mailbox"):
        mailbox["mailbox"] = agent_id
    built["endpoints"] = mailbox
    digest = env.get("CITY_IMAGE_DIGEST", "sha256:local")
    built["build"] = {
        "image_digest": digest,
        "git_sha": env.get("CITY_GIT_SHA", "dev"),
        "sdk_version": "0.9.0",
    }
    built["dependencies"] = {"models": []}
    return built


def _empty_child(lines: list[str], index: int, indent: int) -> dict[str, Any] | list[Any]:
    for raw in lines[index + 1 :]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        nxt = len(raw) - len(raw.lstrip(" "))
        if nxt <= indent:
            return {}
        if raw.strip().startswith("- "):
            return []
        return {}
    return {}


def _scalar(value: str) -> object:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    if value == "true":
        return True
    if value == "false":
        return False
    if value.isdigit():
        return int(value)
    return value
