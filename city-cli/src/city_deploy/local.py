"""Local project helpers: template copy and in-process chat."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO, cast

from cap.core.errors import CapError
from cap.core.timeutil import format_time
from city_sdk.boot import (
    brain_stamp,
    llm_connector,
    prepare,
    reload_brain,
)
from city_sdk.city import Peer
from city_sdk.devmode import CITIZEN_SENDER, invoke_local
from city_sdk.gateway_env import inject_team_gateway
from city_sdk.harness import Harness
from city_sdk.ports import LocalLedger, MemoryIndex, MemoryRelay
from city_sdk.utils.logging import line_from_record
from rich.markup import escape

from city_deploy.agent_ids import new_rid
from city_deploy.card_schema import write_card_id
from city_deploy.chat_play import (
    CHARACTERS,
    SESSION_COMMANDS,
    PlayMove,
    agent_part,
    explain_turn,
    interpret,
    line_hint,
    moves_for,
)
from city_deploy.style import console, echo_input_color, report, say, transcript

_DEFAULT_PROCEDURE = "visita-cardiologica"
_LOCAL_RX_PREFIX = "rx-local-"


def _local_catalogue(service: str) -> tuple[str, str, bool]:
    """Procedure code, specialty, and whether the catalogue row needs a prescription."""
    if service.startswith("family_counseling"):
        return ("CONS-GIN", "gynecology", False)
    if service.startswith("hospital") or service == "lab":
        return ("VIS-FIS", "physiatry", True)
    return ("VIS-CARD", "cardiology", True)


# One template for every role (ADR-110). The team sets role and service_types on the card.
TEMPLATE = "agent"
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
    team_id: str,
    rid: Callable[[], str] | None = None,
) -> dict[str, str]:
    """Scaffold ``root/<name>/`` from the agent template.

    ``--root`` is the parent directory. The agent project always lands in a
    new folder named ``name``. ``team_id`` (from ``city login``) replaces the
    template placeholder in ``card.yaml`` / ``city.yaml``. ``agent_id`` and
    ``endpoints.mailbox`` are both ``agent:<rid>``, a fresh 12-character id.
    Role and ``service_types`` stay as the template wrote them: the team edits
    ``card.yaml`` (and the same ``role`` in ``city.yaml``) before deploy.
    Team credentials stay in the user config store
    (``~/.config/city/teams/<team>/``), not inside the project.
    """
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
    write_card_id(dest / "card.yaml", f"agent:{(rid or new_rid)()}")
    return {
        "template": TEMPLATE,
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


def flat_field(text: str, key: str) -> str | None:
    prefix = key + ":"
    for line in text.splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip().strip('"')
    return None


def _prepare_local_harness(root: Path) -> tuple[Harness, str | None]:
    """Load the project harness for local-only modes (serve / chat)."""
    if os.environ.get("CITY_ENV") == "cloud":
        raise CapError("POLICY_VIOLATION", "CITY_ENV")
    project = root.resolve()
    os.environ["CITY_CARD"] = str(project / "card.yaml")
    os.environ.pop("CITY_ROLE", None)
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


class _AgentLogs(logging.Handler):
    """Brain and SDK lines emitted while a turn runs. Transport libraries stay out."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(
            ("httpx", "httpcore", "urllib3", "asyncio", "uvicorn", "hpack", "concurrent")
        ):
            return
        self.records.append(record)


def _arm_agent_logs() -> tuple[_AgentLogs, int, list[logging.Handler]]:
    """Collect every level. Root keeps only this handler, so a line is not also dumped raw."""
    root = logging.getLogger()
    handler = _AgentLogs()
    previous = root.level
    previous_handlers = list(root.handlers)
    root.handlers = [handler]
    root.setLevel(logging.DEBUG)
    return handler, previous, previous_handlers


def _disarm_agent_logs(
    handler: _AgentLogs,
    previous: int,
    previous_handlers: list[logging.Handler],
) -> None:
    root = logging.getLogger()
    root.removeHandler(handler)
    root.setLevel(previous)
    root.handlers = previous_handlers


class _LineCapture(io.TextIOBase):
    """Lines written by ``print`` while the brain runs."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[tuple[float, str]] = []
        self._pending = ""

    def write(self, s: str) -> int:
        self._pending += s
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            if line.strip():
                self.lines.append((time.time(), line))
        return len(s)

    def flush(self) -> None:
        if self._pending.strip():
            self.lines.append((time.time(), self._pending))
        self._pending = ""

    def isatty(self) -> bool:
        return False


class _StdioGrab:
    def __init__(self) -> None:
        self.stdout = _LineCapture()
        self.stderr = _LineCapture()


@contextmanager
def _capture_stdio() -> Iterator[_StdioGrab]:
    """Take ``print()`` (and stderr) for one turn. The chat chrome keeps the real streams."""
    grab = _StdioGrab()
    previous_out, previous_err = sys.stdout, sys.stderr
    sys.stdout = cast(TextIO, grab.stdout)
    sys.stderr = cast(TextIO, grab.stderr)
    try:
        yield grab
    finally:
        grab.stdout.flush()
        grab.stderr.flush()
        sys.stdout = previous_out
        sys.stderr = previous_err


def _grabbed_prints(grab: _StdioGrab) -> list[tuple[float, str, str]]:
    rows = [(when, "stdout", text) for when, text in grab.stdout.lines]
    rows.extend((when, "stderr", text) for when, text in grab.stderr.lines)
    return rows


def _isatty(stream: TextIO) -> bool:
    check = getattr(stream, "isatty", None)
    try:
        return bool(check and check())
    except (AttributeError, ValueError, OSError):
        return False


def _read_chat_line(stdin: TextIO, stdout: TextIO, label: str) -> str:
    """Read one line. On a terminal the prompt and the echo are the input color."""
    interactive = _isatty(stdin) and _isatty(stdout)
    if interactive:
        console.print(f"[you]{escape(label)}>[/you] ", end="")
        console.file.flush()
        echo_input_color(stdout, on=True)
    try:
        line = stdin.readline()
    finally:
        if interactive:
            echo_input_color(stdout, on=False)
    if line.strip() and not interactive:
        transcript("you", line.strip())
    return line


def _print_legend() -> None:
    console.print(
        "[you]in[/you]   what you type     "
        "[log]log[/log]  agent logs     "
        "[out]out[/out]  what the agent answers"
    )
    console.print(
        "[ask]citizen >[/ask]  sent to the model     [reply]agent >[/reply]  the model's reply"
    )


def _print_agent_logs(
    lines: Sequence[Mapping[str, object]],
    records: Sequence[logging.LogRecord],
    prints: Sequence[tuple[float, str, str]] = (),
) -> None:
    """Harness lines, ``logging`` records, and ``print()`` lines, in time order."""
    blocks: list[tuple[str, str, str, str | None]] = []
    messages: set[str] = set()
    for line in lines:
        severity, text = _format_chat_log(line)
        blocks.append((str(line.get("time", "")), severity, text, _chat_style(severity)))
        raw = line.get("message")
        if isinstance(raw, str) and raw:
            messages.add(raw)
    for record in records:
        built = line_from_record(record)
        # The harness binds category=delivery for the whole turn. The logger name
        # is what tells logging.debug apart from a harness line.
        built["category"] = record.name
        severity, text = _format_chat_log(built)
        blocks.append(
            (
                str(built.get("time", "")),
                severity,
                text,
                _chat_style(severity, getattr(record, "llm_turn", None)),
            )
        )
        messages.add(record.getMessage())
    for when, stream, text in prints:
        if _logging_echo(text, messages):
            continue
        stamp = datetime.fromtimestamp(when, UTC).isoformat()
        clock = stamp[11:19] if len(stamp) >= 19 else stamp
        label = "PRINT" if stream == "stdout" else "STDERR"
        blocks.append((stamp, "INFO", f"{clock} {label} {text}", None))
    blocks.sort(key=lambda item: item[0])
    for _stamp, _severity, text, style in blocks:
        transcript("log", text, style=style)


def _logging_echo(line: str, messages: set[str]) -> bool:
    """True when ``line`` is a logging handler's copy of a record we already show."""
    head = line.lstrip()
    if not head.startswith(("DEBUG:", "INFO:", "WARNING:", "ERROR:", "CRITICAL:")):
        return False
    return any(message and message in line for message in messages)


def _chat_style(severity: str, turn: object = None) -> str | None:
    """Hue for one log row. A failure stays red. A model prompt and reply take their own."""

    if severity in {"ERROR", "CRITICAL"}:
        return "bad"
    if severity == "WARNING":
        return "warn"
    if turn == "in":
        return "ask"
    if turn == "out":
        return "reply"
    return None


def _format_chat_log(line: Mapping[str, object]) -> tuple[str, str]:
    """One short log for the chat. The header already names the agent, team, and market."""
    stamp = str(line.get("time", ""))
    clock = stamp[11:19] if len(stamp) >= 19 else stamp
    severity = str(line.get("severity", "INFO"))
    level = {"WARNING": "WARN", "CRITICAL": "CRIT"}.get(severity, severity)
    message = str(line.get("message", "")).replace("\n", " ").strip()
    category = str(line.get("category", "") or "")
    if message == category:
        category = ""
    tag = f"[{category}]" if category else ""
    head = " ".join(part for part in (clock, level, tag, message) if part)
    details: list[str] = []
    for key, label in (
        ("type", "type"),
        ("msg_id", "msg"),
        ("need_id", "need"),
        ("conversation_id", "conversation"),
    ):
        value = line.get(key)
        if not isinstance(value, str) or not value:
            continue
        shown = value[:8] if key == "msg_id" and len(value) > 8 else value
        details.append(f"{label}={shown}")
    body = head if not details else f"{head}\n{'  '.join(details)}"
    exception = line.get("exception")
    summary = _exception_summary(exception) if isinstance(exception, str) else ""
    if summary:
        body = f"{body}\n{summary}"
    return severity, body


def _exception_summary(text: str) -> str:
    """The error line and the innermost frame, not the wrapped traceback."""
    rows = [row.strip() for row in text.splitlines() if row.strip()]
    if not rows:
        return ""
    error = rows[-1]
    frame = ""
    for row in reversed(rows):
        if not row.startswith("File "):
            continue
        path, _, rest = row[5:].partition(",")
        path = path.strip().strip('"')
        name = path.rsplit("/", 1)[-1]
        line_no = ""
        func = ""
        if "line " in rest:
            line_no = rest.split("line ", 1)[1].split(",", 1)[0].strip()
        if " in " in rest:
            func = rest.rsplit(" in ", 1)[-1].strip()
        frame = " ".join(part for part in (f"{name}:{line_no}" if line_no else name, func) if part)
        break
    return f"{error}\n{frame}" if frame else error


def _print_chat_actions(
    result: dict[str, Any],
    posts: list[dict[str, object]],
    *,
    target: str,
) -> None:
    console.print()
    status = str(result.get("status", "unknown"))
    actions = result.get("actions")
    rows = actions if isinstance(actions, list) else []
    chunks: list[str] = [status]
    if not rows:
        chunks.append("(no actions)")
    chunks.extend(explain_turn(rows, posts, target=target))
    transcript("out", "\n".join(chunks))
    console.print()


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
    _print_legend()
    _print_agent_logs(harness.log.lines, [])
    capture, previous_level, previous_handlers = _arm_agent_logs()
    try:
        _print_characters()
        actor: str | None = None
        moves = moves_for("citizen", target, service=service)
        while True:
            try:
                label = "as" if actor is None else actor
                line = _read_chat_line(stdin, stdout, label)
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
            if reload_brain(harness, project, stamp=stamp):
                stamp = brain_stamp(project)
                say("Brain reloaded — agent sources changed", kind="info")
            posted = len(harness.relay.posted) if isinstance(harness.relay, MemoryRelay) else 0
            logged = len(harness.log.lines)
            capture.records.clear()
            result: dict[str, Any] | None = None
            error: CapError | None = None
            with _capture_stdio() as grab:
                try:
                    result = await invoke_local(harness, payload)
                except CapError as exc:
                    error = exc
            # Restore the real stdout before drawing the turn. Otherwise the
            # chat lines fall into the print() capture.
            _print_agent_logs(harness.log.lines[logged:], capture.records, _grabbed_prints(grab))
            capture.records.clear()
            if error is not None or result is None:
                if error is not None:
                    say(f"Invoke failed  [mute]{error.code}  {error.message}[/mute]", kind="warn")
                continue
            fresh: list[dict[str, object]] = []
            if isinstance(harness.relay, MemoryRelay):
                fresh = [row for row in harness.relay.posted[posted:] if isinstance(row, dict)]
            _print_chat_actions(result, fresh, target=target)
    finally:
        _disarm_agent_logs(capture, previous_level, previous_handlers)


def _print_characters() -> None:
    console.print()
    console.print(
        "[label]Who are you?[/label]  Type a number or a name. /as <name> switches later."
    )
    for index, (name, meaning) in enumerate(CHARACTERS, start=1):
        console.print(f"  [label]{index}[/label]  {name} — {meaning}")
    _print_session_commands()


def _print_session_commands() -> None:
    console.print()
    for command, meaning in SESSION_COMMANDS:
        console.print(f"  [label]{command}[/label]  {meaning}")


def _print_moves(actor: str, target: str, moves: list[PlayMove]) -> None:
    console.print()
    console.print(
        f"[label]Playing[/label] [you]{escape(actor)}[/you] → [you]{escape(target)}[/you]"
    )
    if not moves:
        say(
            "A sentence still reaches the brain. /as <name> switches.",
            kind="warn",
        )
        _print_session_commands()
        return
    console.print("Type a number, that slash command, or a sentence.")
    console.print()
    for index, move in enumerate(moves, start=1):
        if not isinstance(move, PlayMove):
            continue
        console.print(
            f"  [label]{index}[/label]  [label]{escape(move.command)}[/label]  {escape(move.title)}"
        )
        console.print(f"     {escape(move.meaning)}")
        console.print()
    console.print(line_hint(actor))
    _print_session_commands()
