"""Local project helpers: template copy, one in-process need, and a loopback harness."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import sys
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any, TextIO

import uvicorn
from cap.core.errors import CapError
from cap.core.http_ports import listen
from cap.core.timeutil import format_time
from city_sdk.app import create_app
from city_sdk.boot import (
    BRAIN_SPEC,
    brain_stamp,
    llm_connector,
    load_brain,
    plane_for,
    prepare,
    reload_brain,
)
from city_sdk.city import City, Message, Need, Peer
from city_sdk.devmode import CITIZEN_SENDER, invoke_local
from city_sdk.gateway_env import inject_team_gateway, load_team_gateway
from city_sdk.harness import Harness
from city_sdk.logs import HarnessLog
from city_sdk.ports import LocalLedger, MemoryIndex, MemoryRelay
from city_sdk.utils.logging import configure, uvicorn_log_config

from city_deploy.agent_ids import new_rid
from city_deploy.card_schema import write_card_id
from city_deploy.chat_play import (
    CHARACTERS,
    SESSION_COMMANDS,
    PlayMove,
    agent_part,
    explain_turn,
    interpret,
    moves_for,
)
from city_deploy.style import report, say

_DEFAULT_PROCEDURE = "visita-cardiologica"
_LOCAL_RX_PREFIX = "rx-local-"


def _local_catalogue(service: str) -> tuple[str, str, bool]:
    """Procedure code, specialty, and whether the catalogue row needs a prescription."""
    if service.startswith("family_counseling"):
        return ("CONS-GIN", "gynecology", False)
    if service.startswith("hospital") or service == "lab":
        return ("VIS-FIS", "physiatry", True)
    return ("VIS-CARD", "cardiology", True)


# One template for every role (ADR-110). A role sets the card's role and service types.
TEMPLATE = "agent"
ROLE_CARDS: dict[str, tuple[str, str]] = {
    "booking_centre": ("booking_centre", "booking_centre"),
    "gp": ("gp", "gp"),
    "hospital": ("facility", "hospital.physiatry"),
    "specialist_care": ("facility", "specialist_care.cardiology"),
    "family_counseling": ("facility", "family_counseling.gynecology"),
}
_LOCAL_PEER = Peer("agent:t05/osp-x", service="lab", district=8, role="hospital")
_SLUG = "agent-uno"
_TEAM = "t07"


def templates_root() -> Path:
    here = Path(__file__).resolve()
    packaged = here.parent / "templates"
    if (packaged / TEMPLATE).is_dir():
        return packaged
    for parent in here.parents:
        if (parent / "templates" / TEMPLATE).is_dir():
            return parent / "templates"
    raise CapError("SCHEMA_INVALID", "templates")


def init_project(
    root: Path,
    name: str,
    *,
    role: str = "booking_centre",
    team_id: str,
    rid: Callable[[], str] | None = None,
) -> dict[str, str]:
    """Scaffold ``root/<name>/`` from the agent template, for ``role``.

    ``--root`` is the parent directory. The agent project always lands in a
    new folder named ``name``. ``team_id`` (from ``city login``) replaces the
    template placeholder in ``card.yaml`` / ``city.yaml``. ``agent_id`` and
    ``endpoints.mailbox`` are both ``agent:<rid>``, a fresh 12-character id.
    Team credentials stay in the user config store
    (``~/.config/city/teams/<team>/``), not inside the project.
    """
    if role not in ROLE_CARDS:
        raise CapError("SCHEMA_INVALID", "role")
    if re.fullmatch(r"[a-z0-9_-]+", name) is None:
        raise CapError("SCHEMA_INVALID", "name")
    if re.fullmatch(r"[a-z0-9_-]+", team_id) is None:
        raise CapError("SCHEMA_INVALID", "team")
    source = templates_root() / TEMPLATE
    root.mkdir(parents=True, exist_ok=True)
    dest = (root / name).resolve()
    if dest.exists():
        if dest.is_file() or any(dest.iterdir()):
            raise CapError("DUPLICATE", str(dest))
    else:
        dest.mkdir(parents=True)
    slug = _SLUG
    placeholder = _TEAM
    for path in source.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        target = dest / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text(encoding="utf-8")
        if path.suffix in {".yaml", ".yml", ".md"} or path.name == "Dockerfile":
            text = _rewrite_scaffold(
                text,
                slug=slug,
                name=name,
                placeholder=placeholder,
                team_id=team_id,
            )
        target.write_text(text, encoding="utf-8")
    # ADR-070: the hall Agents island shows card.name; keep it equal to the agent slug.
    _set_yaml_field(dest / "card.yaml", "name", name)
    card_role, service_type = ROLE_CARDS[role]
    _set_yaml_field(dest / "card.yaml", "role", card_role)
    _set_yaml_list(dest / "card.yaml", "service_types", [service_type])
    _set_yaml_field(dest / "city.yaml", "role", card_role)
    write_card_id(dest / "card.yaml", f"agent:{(rid or new_rid)()}")
    return {
        "template": TEMPLATE,
        "role": role,
        "name": name,
        "team": team_id,
        "path": str(dest),
    }


def _rewrite_scaffold(
    text: str,
    *,
    slug: str,
    name: str,
    placeholder: str,
    team_id: str,
) -> str:
    """Replace template agent slug and team id with the caller's values."""
    rewritten = text.replace(f"agent:{placeholder}/", f"agent:{team_id}/")
    rewritten = rewritten.replace(f"team: team:{placeholder}", f"team: team:{team_id}")
    rewritten = rewritten.replace(f"team: {placeholder}", f"team: {team_id}")
    return rewritten.replace(slug, name)


def _set_yaml_field(path: Path, key: str, value: str) -> None:
    """Set a top-level ``key:`` line in a flat YAML file (scaffold cards)."""

    text = path.read_text(encoding="utf-8")
    prefix = f"{key}:"
    lines = text.splitlines()
    out: list[str] = []
    found = False
    for line in lines:
        if line.startswith(prefix):
            out.append(f"{prefix} {value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.insert(0, f"{prefix} {value}")
    path.write_text(
        "\n".join(out) + ("\n" if text.endswith("\n") or not text else ""), encoding="utf-8"
    )


def _set_yaml_list(path: Path, key: str, values: list[str]) -> None:
    """Replace a top-level ``key:`` block list in a flat YAML file."""

    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    skipping = False
    for line in lines:
        if skipping and line.startswith("  - "):
            continue
        skipping = False
        if line == f"{key}:":
            out.append(line)
            out.extend(f"  - {value}" for value in values)
            skipping = True
            continue
        out.append(line)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def flat_field(text: str, key: str) -> str | None:
    prefix = key + ":"
    for line in text.splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip().strip('"')
    return None


async def run_local_need(root: Path) -> dict[str, Any]:
    """One citizen message through the project brain, against local stand-ins."""

    city_yaml = (root / "city.yaml").read_text(encoding="utf-8")
    if flat_field(city_yaml, "brain") is not None:
        raise CapError("SCHEMA_INVALID", "brain")
    team = flat_field(city_yaml, "team")
    if team:
        os.environ.setdefault("CITY_TEAM", team)
    inject_team_gateway(team_id=team, root=root)
    function = load_brain(root, BRAIN_SPEC)
    llm = llm_connector({}, team_id=team, root=root)
    agent_id = "agent:local/dev"
    relay = MemoryRelay(agent_id=agent_id, ledger=LocalLedger())
    inbound = Message(
        type="chat",
        sender=CITIZEN_SENDER,
        recipient=agent_id,
        msg_id="local-1",
        need_id="local",
        body={"text": "I need a visit"},
    )
    relay.record(inbound)
    market = City(
        index=MemoryIndex(peers=[_LOCAL_PEER]),
        messages=relay,
        history=relay,
        agent_id=agent_id,
        inbound=inbound,
        llm=llm,
    )
    await function(Need(summary=inbound.text, message=inbound), market)
    said = [str(row["to"]) for row in relay.posted]
    asked = next((to for to in said if to != CITIZEN_SENDER), None)
    return {"asked": asked, "said": said}


def _prepare_local_harness(root: Path) -> tuple[Harness, str | None]:
    """Load the project harness for local-only modes (serve / chat)."""
    if os.environ.get("CITY_ENV") == "cloud":
        raise CapError("POLICY_VIOLATION", "CITY_ENV")
    project = root.resolve()
    ready = prepare(
        {
            "CITY_CARD": str(project / "card.yaml"),
            "CITY_CITY": str(project / "city.yaml"),
        }
    )
    if ready is None:
        raise CapError("SCHEMA_INVALID", "card")
    city_text = (project / "city.yaml").read_text(encoding="utf-8")
    team = flat_field(city_text, "team")
    if team:
        os.environ.setdefault("CITY_TEAM", team)
    inject_team_gateway(team_id=team, root=project)
    harness, _plane = ready
    harness.llm = llm_connector({}, team_id=team, root=project)
    return harness, team


def card_service(harness: Harness) -> str:
    raw = harness.card.get("service_types")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item:
                return item
    return "lab"


def seed_chat_world(harness: Harness) -> None:
    """A peer for ``find`` and one slot for the stand-in Ledger, so a brain can act without
    a live wave. Family counseling offers only free-access procedures of its specialty."""
    if not isinstance(harness.index, MemoryIndex):
        raise CapError("POLICY_VIOLATION", "index")
    if _LOCAL_PEER.agent_id not in {peer.agent_id for peer in harness.index.peers}:
        harness.index.peers.append(_LOCAL_PEER)
    ledger = _local_ledger(harness)
    if ledger is None or ledger.slots:
        return
    procedure, _specialty, _needs_rx = _local_catalogue(card_service(harness))
    ledger.slots.append(
        {
            "slot_id": "slot-local-1",
            "doctor_id": "doc-local-1",
            "facility": harness.agent_id,
            "procedure": procedure,
            "agenda_date": format_time(harness.clock.now() + timedelta(days=1)),
            "agenda_day": "mon",
        }
    )


async def seed_local_rx(harness: Harness) -> str:
    """Issue one prescription on the stand-in Ledger; return its number."""
    ledger = _local_ledger(harness)
    if ledger is None:
        raise CapError("POLICY_VIOLATION", "ledger")
    number = f"{_LOCAL_RX_PREFIX}{len(ledger.prescriptions) + 1}"
    procedure = _DEFAULT_PROCEDURE
    if ledger.slots:
        raw = ledger.slots[0].get("procedure")
        if isinstance(raw, str) and raw:
            procedure = raw
    ledger.issue(number, procedure)
    return number


def _local_ledger(harness: Harness) -> LocalLedger | None:
    relay = harness.relay
    if not isinstance(relay, MemoryRelay):
        return None
    if relay.ledger is None:
        relay.ledger = LocalLedger()
    return relay.ledger


def chat_line_to_payload(
    line: str,
    *,
    service: str,
    sender: str = CITIZEN_SENDER,
) -> dict[str, object] | None:
    """Map a chat line to an invoke payload. ``None`` means a meta-command handled by the REPL."""
    text = line.strip()
    if not text:
        return None
    if text in {"/quit", "/exit", "/q"}:
        return None
    if text == "/rx":
        return None
    if text.startswith("/emergency"):
        rest = text[len("/emergency") :].strip() or "emergency"
        return {
            "type": "need.request",
            "from": sender,
            "service": service,
            "summary": rest,
            "is_emergency": True,
            "body": {"service": service, "summary": rest, "text": rest, "is_emergency": True},
        }
    if text.startswith("/checkin"):
        booking_id = text[len("/checkin") :].strip()
        if not booking_id:
            raise CapError("SCHEMA_INVALID", "booking_id")
        return {
            "type": "checkin",
            "from": sender,
            "body": {"booking_id": booking_id},
        }
    if text.startswith("/confirm"):
        fields = _chat_pairs(text[len("/confirm") :].strip())
        offer_id = fields.get("offer_id") or fields.get("offer")
        if not offer_id:
            raise CapError("SCHEMA_INVALID", "offer")
        confirmed: dict[str, object] = {"offer_id": offer_id}
        if "consent" in fields:
            confirmed["consent_ref"] = fields["consent"]
        return {"type": "confirm", "from": sender, "body": confirmed}
    if text.startswith("/offer"):
        fields = _chat_pairs(text[len("/offer") :].strip())
        procedure = fields.get("procedure", "")
        return {
            "type": "offer",
            "from": sender,
            "body": {
                "offer_id": fields.get("offer_id", "of-slot-local-1"),
                "slot_id": fields.get("slot_id", "slot-local-1"),
                "procedure": procedure,
                "priority": fields.get("priority", "B"),
            },
        }
    if text.startswith("/cancel"):
        fields = _chat_pairs(text[len("/cancel") :].strip())
        offer_id = fields.get("offer_id") or fields.get("offer")
        if not offer_id:
            raise CapError("SCHEMA_INVALID", "offer")
        return {
            "type": "cancel",
            "from": sender,
            "body": {"offer_id": offer_id, "reason": fields.get("reason", "slot_gone")},
        }
    if text.startswith("/data"):
        fields = _chat_pairs(text[len("/data") :].strip())
        return {
            "type": "data.response",
            "from": sender,
            "body": {"purpose": "booking", "provided": dict(fields)},
        }
    if text.startswith("/json"):
        raw = text[len("/json") :].strip()
        if not raw:
            raise CapError("SCHEMA_INVALID", "json")
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CapError("SCHEMA_INVALID", "json") from exc
        if not isinstance(loaded, dict):
            raise CapError("SCHEMA_INVALID", "json")
        return loaded
    if text.startswith("/need"):
        rest = text[len("/need") :].strip()
        need_body: dict[str, object] = {"service": service}
        for part in rest.split():
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            if key == "rx":
                need_body["prescription_number"] = value
            elif key == "symptom":
                need_body["symptom_code"] = value
            elif key in {"service", "summary", "text", "procedure", "priority", "service_type"}:
                need_body[key] = value
        return {
            "type": "need.request",
            "from": sender,
            "service": service,
            "summary": str(need_body.get("summary", rest or "need")),
            "body": need_body,
        }
    if text.startswith("/"):
        raise CapError("SCHEMA_INVALID", "command")
    return {
        "type": "need.request",
        "from": sender,
        "service": service,
        "summary": text,
        "body": {"service": service, "summary": text, "text": text},
    }


def _chat_pairs(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in text.split():
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key and value:
            fields[key] = value
    return fields


def _print_chat_actions(
    result: dict[str, Any],
    posts: list[dict[str, object]],
    *,
    target: str,
) -> None:
    status = result.get("status", "unknown")
    say(f"Agent  [city]{status}[/city]", kind="ok" if status == "processed" else "warn")
    actions = result.get("actions")
    rows = actions if isinstance(actions, list) else []
    if not rows:
        say("  (no actions)", kind="info")
    for action in rows:
        if not isinstance(action, dict):
            continue
        kind = str(action.get("kind", "?"))
        bits = [f"{key}={value}" for key, value in action.items() if key != "kind"]
        detail = " ".join(bits)
        say(f"  → {kind}" + (f"  {detail}" if detail else ""), kind="info")
    for sentence in explain_turn(rows, posts, target=target):
        say(f"  {sentence}", kind="info")


def chat_local(
    root: Path,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> None:
    """Interactive in-process chat with the local harness (no city mailbox)."""
    asyncio.run(_chat_local_async(root, stdin=stdin or sys.stdin, stdout=stdout or sys.stdout))


async def _chat_local_async(
    root: Path,
    *,
    stdin: TextIO,
    stdout: TextIO,
) -> None:
    project = root.resolve()
    harness, _team = _prepare_local_harness(project)
    seed_chat_world(harness)
    service = card_service(harness)
    target = agent_part(harness.card)
    stamp = brain_stamp(project)
    await harness.start()
    report(
        "Local chat",
        [
            ("Agent", harness.agent_id),
            ("Part", target),
            ("Service", service),
            ("Inbox", "in-memory (not the city mailbox)"),
            ("Peer", _LOCAL_PEER.agent_id),
            ("Reload", "agent sources on change"),
        ],
        subtitle="Pick who you are, then a move, slash command, or sentence",
    )
    _print_characters()
    actor: str | None = None
    moves = moves_for("citizen", target, service=service)
    while True:
        try:
            stdout.write("as> " if actor is None else f"{actor}> ")
            stdout.flush()
            line = stdin.readline()
        except EOFError:
            break
        if line == "":
            break
        turn = interpret(line, actor=actor, service=service, moves=moves)
        if turn.kind == "noop":
            continue
        if turn.kind == "quit":
            say("Goodbye", kind="info")
            return
        if turn.kind == "error":
            say(turn.message, kind="warn")
            continue
        if turn.kind == "actor":
            if turn.actor is None:
                _print_characters()
                actor = None
                continue
            actor = turn.actor
            moves = moves_for(actor, target, service=service)
            _print_moves(actor, target, moves)
            continue
        if turn.kind == "moves":
            if actor is None:
                _print_characters()
            else:
                _print_moves(actor, target, moves)
            continue
        if turn.kind == "rx":
            try:
                number = await seed_local_rx(harness)
            except CapError as exc:
                say(f"Could not seed Rx  [mute]{exc.code}  {exc.message}[/mute]", kind="warn")
                continue
            say(f"Rx  [city]{number}[/city]  use /need rx={number}", kind="ok")
            continue
        payload = turn.payload
        if payload is None:
            continue
        try:
            if reload_brain(harness, project, stamp=stamp):
                stamp = brain_stamp(project)
                say("Brain reloaded — agent sources changed", kind="info")
            posted = len(harness.relay.posted) if isinstance(harness.relay, MemoryRelay) else 0
            result = await invoke_local(harness, payload)
        except CapError as exc:
            say(f"Invoke failed  [mute]{exc.code}  {exc.message}[/mute]", kind="warn")
            continue
        fresh: list[dict[str, object]] = []
        if isinstance(harness.relay, MemoryRelay):
            fresh = [row for row in harness.relay.posted[posted:] if isinstance(row, dict)]
        _print_chat_actions(result, fresh, target=target)


def _print_characters() -> None:
    say("Who are you? Type a number or a name. /as <name> switches later.", kind="info")
    for index, (name, meaning) in enumerate(CHARACTERS, start=1):
        say(f"  {index}  {name} — {meaning}", kind="info")
    _print_session_commands()


def _print_session_commands() -> None:
    for command, meaning in SESSION_COMMANDS:
        say(f"  {command} — {meaning}", kind="info")


def _print_moves(actor: str, target: str, moves: list[PlayMove]) -> None:
    say(f"Playing [city]{actor}[/city] → [city]{target}[/city].", kind="ok")
    if not moves:
        say(
            "This character sends no care-journey message to that agent. "
            "A sentence or /json {...} still reaches the brain. /as <name> switches.",
            kind="warn",
        )
        _print_session_commands()
        return
    say("Type a number, that slash command, or a sentence.", kind="info")
    for index, move in enumerate(moves, start=1):
        if not isinstance(move, PlayMove):
            continue
        say(f"  {index}  {move.command} — {move.title}", kind="info")
        say(f"     {move.meaning}", kind="info")
    say(
        "A sentence is a need.request. It carries your words only, "
        "not a symptom code or a prescription.",
        kind="info",
    )
    _print_session_commands()


def serve_local(
    root: Path,
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
    as_json: bool = False,
) -> dict[str, Any]:
    """Serve the project harness on loopback. ``/dev/invoke`` stays in memory."""
    _require_loopback(host)
    if port is not None and (port < 1 or port > 65535):
        raise CapError("SCHEMA_INVALID", "port")
    harness, team = _prepare_local_harness(root)
    key, _url = load_team_gateway(team_id=team, root=root.resolve())
    llm_status = "loaded" if key else "missing"
    if not isinstance(harness.index, MemoryIndex):
        raise CapError("POLICY_VIOLATION", "index")
    harness.index.peers.append(_LOCAL_PEER)
    raw_role = harness.card.get("role")
    role_name = raw_role if isinstance(raw_role, str) else ""
    plane = plane_for(role_name)
    chosen = listen(plane, environ={"HOST": host})[1] if port is None else port
    role = role_name if role_name else plane.value
    facts = {
        "agent_id": harness.agent_id,
        "role": role,
        "host": host,
        "port": chosen,
        "url": _http_base(host, chosen),
        "invoke": "POST /dev/invoke",
        "inbox": "local",
        "peer": _LOCAL_PEER.agent_id,
    }
    if as_json:
        print(json.dumps(facts), flush=True)
    else:
        report(
            "Local agent",
            [
                ("Agent", str(facts["agent_id"])),
                ("Listen", str(facts["url"])),
                ("Invoke", "POST /dev/invoke"),
                ("Inbox", "in-memory (not the city mailbox)"),
                ("Peer", _LOCAL_PEER.agent_id),
            ],
            subtitle="curl this process — nothing leaves the laptop",
        )
        say(f"Listening on [city]{facts['url']}[/city]", kind="info")
        if llm_status == "loaded":
            gateway = os.environ.get("CITY_LLM_GATEWAY_URL", "").strip() or "llm"
            say(f"LLM  [city]{gateway}[/city]  key from city login", kind="info")
        elif llm_status == "missing":
            say(
                "LLM gateway key missing — run city login "
                "(downloads the team VK for https://llm.projectnandaitaly.ai)",
                kind="warn",
            )
    human = not as_json
    harness.log = HarnessLog(stream=sys.stdout, now=harness.started, console=human)
    configure(console=human)
    asyncio.run(harness.start())
    uvicorn.run(
        create_app(harness, dev=True),
        host=host,
        port=chosen,
        log_config=uvicorn_log_config(console=human),
    )
    return facts


def _require_loopback(host: str) -> None:
    name = host.strip().strip("[]").lower()
    if name in {"localhost", "localhost.localdomain"}:
        return
    try:
        address = ipaddress.ip_address(name)
    except ValueError as exc:
        raise CapError("POLICY_VIOLATION", "host") from exc
    if not address.is_loopback:
        raise CapError("POLICY_VIOLATION", "host")


def _http_base(host: str, port: int) -> str:
    shown = f"[{host}]" if ":" in host else host
    return f"http://{shown}:{port}"
