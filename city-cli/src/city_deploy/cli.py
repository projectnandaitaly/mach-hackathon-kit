"""``city`` CLI. Human text by default, JSON when ``--json`` is set."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import webbrowser
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx
import typer
from cap.core.envelope import verify
from cap.core.errors import CapError
from cap.core.grant import SignedGrant, issue
from cap.core.keys import PublicKey
from cap.core.timeutil import format_time
from city_index.families import family_of, role_permissions
from httpx import ASGITransport

from city_deploy.agent_ids import ID_FAMILIES, id_family, id_name
from city_deploy.api import create_app
from city_deploy.card_schema import (
    UNMINTED,
    bump_card_yaml,
    card_from_yaml,
    load_card_yaml,
    unminted_id,
    validate_mhm_card,
    write_card_id,
)
from city_deploy.conform import select_profile
from city_deploy.controller import ensure_controller_key, load_controller
from city_deploy.image import collect_context, encode_files, parse_dockerfile
from city_deploy.local import chat_local, flat_field, init_project, run_local_need, serve_local
from city_deploy.loopback import LoopbackReceiver, new_pkce
from city_deploy.service import UNASSIGNED, DeployService
from city_deploy.store import TeamStore, resolve_active_team, write_active_team
from city_deploy.style import Kind as StyleKind
from city_deploy.style import (
    celebrate_login,
    console,
    format_duration,
    line_mark,
    replace_status,
    report,
    say,
)
from city_deploy.style import fail as style_fail
from city_deploy.urls import DEFAULT_CITY_PLATFORM_URL, resolve_platform_url

_POLL_ATTEMPTS = 90
_POLL_SLEEP_S = 2.0
# Nothing renews a Deployment Grant: an agent stops at not_after until the next release.
_GRANT_HOURS = 7 * 24
_LOOPBACK_TIMEOUT_S = 300.0

_HELP = (
    "[city]city[/city] — deploy agents to [city]Milano Agentic City[/city].\n"
    "[mute]Controller keys stay on your machine. No cloud credentials required.[/mute]"
)
_EPILOG = (
    f"API  {DEFAULT_CITY_PLATFORM_URL}\n"
    "Local  --api-url local    Scripting  --json"
)

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
    pretty_exceptions_show_locals=False,
    help=_HELP,
    epilog=_EPILOG,
)
init_app = typer.Typer(no_args_is_help=True, add_completion=False, rich_markup_mode="rich")
keys_app = typer.Typer(no_args_is_help=True, add_completion=False, rich_markup_mode="rich")
card_app = typer.Typer(no_args_is_help=True, add_completion=False, rich_markup_mode="rich")
secrets_app = typer.Typer(no_args_is_help=True, add_completion=False, rich_markup_mode="rich")
llm_app = typer.Typer(no_args_is_help=True, add_completion=False, rich_markup_mode="rich")
llm_key_app = typer.Typer(no_args_is_help=True, add_completion=False, rich_markup_mode="rich")
app.add_typer(init_app, name="init")
app.add_typer(keys_app, name="keys")
app.add_typer(card_app, name="card")
app.add_typer(secrets_app, name="secrets")
app.add_typer(llm_app, name="llm")
llm_app.add_typer(llm_key_app, name="key")


@app.callback()
def _root(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json"),
    api_url: str | None = typer.Option(
        None,
        "--api-url",
        help=(
            "Deploy API base URL (default: CITY_PLATFORM_URL / CITY_DEPLOY_URL / "
            f"{DEFAULT_CITY_PLATFORM_URL}; use 'local' for in-process)."
        ),
    ),
) -> None:
    ctx.ensure_object(dict)
    ctx.obj["json"] = as_json
    ctx.obj["api_url"] = resolve_platform_url(api_url, os.environ)


def main() -> None:
    app()


@app.command("login")
def login(
    ctx: typer.Context,
    team: str | None = typer.Option(
        None,
        "--team",
        help=(
            "Required with --api-url local (self-approve). "
            "On the live API the browser chooses the roster team — omit this."
        ),
    ),
    root: Path = typer.Option(Path("."), "--root"),
    no_browser: bool = typer.Option(
        False,
        "--no-browser",
        help="Print a code to approve on another device instead of opening a browser.",
    ),
) -> None:
    """Sign in with your browser (like ``gcloud auth login``)."""
    human = _state(ctx).get("json") is not True
    state = _state(ctx)
    api = state.get("api_url")
    api_label = api if isinstance(api, str) and api else "local (in-process)"
    # In-process / injected ASGI (tests) may self-approve. Live remote never does.
    local = state.get("app") is not None or not isinstance(api, str) or not api
    if local and not team:
        exc = CapError("SCHEMA_INVALID", "--team required for local login")
        _fail(ctx, exc)
        raise typer.Exit(code=1) from exc

    client = _client(ctx, root, token=None)
    if local:
        assert team is not None
        polled = _self_approve(client, team, human=human)
    elif no_browser:
        polled = _code_login(client, human=human)
    else:
        polled = _link_login(client, human=human)
    team_id = str(polled["team_id"])
    # Optional --team is a private expectation only; never echo the other side.
    if team is not None and team_id != team:
        exc = CapError("POLICY_VIOLATION", "team mismatch")
        _fail(ctx, exc)
        raise typer.Exit(code=1) from exc
    store = TeamStore.open(team_id, root=root)
    session_path = store.write_session(str(polled["token"]))
    write_active_team(team_id, root=root)

    key_existed = store.controller_pem_path.is_file()
    authed = _client(ctx, root, token=store.read_session(), team=team_id)
    try:
        public = ensure_controller_key(
            store,
            lambda body: authed.post("/keys/register", body),
            overwrite=False,
        )
    except CapError as exc:
        _fail(ctx, exc)
        raise typer.Exit(code=1) from exc
    key_status = "reused" if key_existed else "created"

    llm_status = "missing"
    try:
        llm_response = authed.get("/teams/me/llm-key")
        _raise_for_status(llm_response)
        llm_body = llm_response.json()
        if isinstance(llm_body, dict):
            virtual_key = llm_body.get("virtual_key")
            if isinstance(virtual_key, str) and virtual_key.strip():
                store.write_llm_key(virtual_key)
                llm_status = "written"
    except CapError:
        llm_status = "missing"

    payload = {
        "team_id": team_id,
        "api": api_label,
        "credentials": str(store.path),
        "session_path": str(session_path.resolve()),
        "controller_key": key_status,
        "controller_pem": str(store.controller_pem_path.resolve()),
        "controller_jwk": str(store.controller_jwk_path.resolve()),
        "llm_key": llm_status,
        "permissions": ["invoke"],
        "public_jwk": {
            "kty": public.get("kty"),
            "crv": public.get("crv"),
            "x": public.get("x"),
        },
    }
    if _state(ctx).get("json") is True:
        typer.echo(json.dumps(payload))
        return
    celebrate_login(team_id)


def _start_device(client: _Client, body: dict[str, Any]) -> dict[str, Any]:
    response = client.post("/login/device", body)
    _raise_for_status(response)
    started = response.json()
    if not isinstance(started, dict):
        raise CapError("SCHEMA_INVALID", "device")
    return started


def _verification_uri(started: dict[str, Any]) -> str:
    return str(started.get("verification_uri", "https://projectnandaitaly.ai/device"))


def _self_approve(client: _Client, team: str, *, human: bool) -> dict[str, Any]:
    del human
    started = _start_device(client, {})
    approve_resp = client.post(
        "/login/approve",
        {"user_code": started["user_code"], "team_id": team, "permissions": ["invoke"]},
    )
    _raise_for_status(approve_resp)
    return _poll_device(client, str(started["device_code"]), human=False)


def _code_login(client: _Client, *, human: bool) -> dict[str, Any]:
    started = _start_device(client, {})
    user_code = str(started["user_code"])
    verify_url = f"{_verification_uri(started)}?code={quote(user_code)}"
    if human:
        say(f"Enter code [city]{user_code}[/city] in your browser", kind="info")
        say(f"Or open [mute]{verify_url}[/mute]", kind="info")
        say("Waiting for you to finish…", kind="info")
    else:
        _pending(verify_url)
    return _poll_device(client, str(started["device_code"]), human=human)


def _link_login(client: _Client, *, human: bool) -> dict[str, Any]:
    pkce = new_pkce()
    with LoopbackReceiver() as receiver:
        started = _start_device(
            client, {"code_challenge": pkce.challenge, "code_challenge_method": "S256"}
        )
        query = urlencode(
            {
                "code": started["user_code"],
                "redirect_uri": receiver.redirect_uri,
                "state": receiver.state,
            }
        )
        link = f"{_verification_uri(started)}?{query}"
        try:
            opened = webbrowser.open(link)
        except Exception:
            opened = False
        if human:
            lead = (
                "Open your browser to finish signing in"
                if opened
                else "Open this link to sign in"
            )
            console.print(f"[info]·[/info]  {lead}")
            typer.echo(f"\n    {link}\n")
            console.print("[mute]Waiting for you to finish…[/mute]")
        else:
            _pending(link)
        try:
            auth_code = receiver.wait(_LOOPBACK_TIMEOUT_S)
        except CapError as exc:
            style_fail(exc.code, exc.message)
            raise typer.Exit(code=1) from exc
    response = client.post(
        "/login/token",
        {
            "device_code": started["device_code"],
            "auth_code": auth_code,
            "code_verifier": pkce.verifier,
        },
    )
    _raise_for_status(response)
    body = response.json()
    if not isinstance(body, dict):
        raise CapError("SCHEMA_INVALID", "token")
    return body


def _pending(link: str) -> None:
    typer.echo(
        json.dumps({"status": "authorization_pending", "verification_uri": link}),
        err=True,
    )


def _poll_device(client: _Client, device_code: str, *, human: bool) -> dict[str, Any]:
    for attempt in range(_POLL_ATTEMPTS):
        response = client.post("/login/poll", {"device_code": device_code})
        if response.status_code < 400:
            body = response.json()
            if isinstance(body, dict):
                return body
            raise CapError("SCHEMA_INVALID", "poll")
        try:
            err = response.json()
        except ValueError:
            err = {}
        code = err.get("code") if isinstance(err, dict) else None
        message = err.get("message") if isinstance(err, dict) else None
        pending = code == "POLICY_VIOLATION" and (
            message == "authorization pending" or message is None
        )
        if pending:
            if human and attempt % 5 == 0 and attempt > 0:
                say("Still waiting for browser sign-in…", kind="info")
            time.sleep(_POLL_SLEEP_S)
            continue
        if isinstance(code, str):
            style_fail(code, message if isinstance(message, str) else "")
        else:
            style_fail("HTTP", response.text.strip() or str(response.status_code))
        raise typer.Exit(code=1)
    style_fail("POLICY_VIOLATION", "authorization timed out")
    raise typer.Exit(code=1)


@keys_app.command("init")
def keys_init(
    ctx: typer.Context,
    team: str | None = typer.Option(None, "--team", help="Team id; default from card.yaml."),
    root: Path = typer.Option(Path("."), "--root"),
) -> None:
    store = _store_for(root, team)
    client = _client(ctx, root, team=store.team_id)
    try:
        public = ensure_controller_key(
            store,
            lambda body: client.post("/keys/register", body),
            overwrite=True,
        )
    except CapError as exc:
        _fail(ctx, exc)
        raise typer.Exit(code=1) from exc
    _emit(
        ctx,
        {**public, "team_id": store.team_id, "credentials": str(store.path)},
        f"Controller key created for team [city]{store.team_id}[/city]",
    )


@keys_app.command("rotate")
def keys_rotate(
    ctx: typer.Context,
    agent: str,
    root: Path = typer.Option(Path("."), "--root"),
) -> None:
    try:
        agent_id, team = _resolve_agent_ref(ctx, agent, root=root)
    except CapError as exc:
        _fail(ctx, exc)
        raise typer.Exit(code=1) from exc
    store = TeamStore.open(team, root=root)
    client = _client(ctx, root, team=team)
    status = client.get(f"/agents/{agent_id}/status")
    _raise_for_status(status)
    version = status.json().get("version")
    if not isinstance(version, str):
        raise typer.Exit(code=1)
    nxt = client.post(f"/agents/{agent_id}/keys/next", {})
    _raise_for_status(nxt)
    public = nxt.json()
    slug = id_name(agent_id)
    grant = {
        "grant_id": f"dg:{team}/{slug}/{version}",
        "agent_id": agent_id,
        "version": version,
        "agent_pubkey": {"kty": public["kty"], "crv": public["crv"], "x": public["x"]},
        "permissions": ["invoke"],
        "not_before": _stamp(ctx, hours=-1),
        "not_after": _stamp(ctx, hours=_GRANT_HOURS),
    }
    signed = issue(load_controller(store), grant, kid=f"team:{team}#controller")
    response = client.post(
        f"/agents/{agent_id}/keys/rotate",
        {"grant": signed.grant, "jws": signed.jws},
    )
    _raise_for_status(response)
    _emit(ctx, response.json(), f"Agent key rotated for [city]{agent_id}[/city]")


@init_app.command("agent")
def init_agent(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="New folder name under --root (agent slug)."),
    template: str = typer.Option(
        "booking_centre",
        "--template",
        help="Starter to copy: booking_centre, facility, gp, specialist, family_counseling, b2b.",
    ),
    root: Path = typer.Option(
        Path("."),
        "--root",
        help="Parent directory. Creates <root>/<name>/ with the template.",
    ),
) -> None:
    """Scaffold a local agent project from a city template."""
    try:
        team_id = resolve_active_team(root=root)
        payload = init_project(root, name, template=template, team_id=team_id)
    except CapError as exc:
        _fail(ctx, exc)
        raise typer.Exit(code=1) from exc
    path = payload["path"]
    _emit(
        ctx,
        payload,
        f"Created [city]{name}[/city] for team [city]{team_id}[/city]  [mute]{path}[/mute]",
        kind="ok",
    )
    if _state(ctx).get("json") is not True:
        say(
            f"Next: [mute]city card validate {name} && city deploy {name}[/mute]",
            kind="info",
        )


@card_app.command("validate")
def card_validate(
    ctx: typer.Context,
    agent: str | None = typer.Argument(
        None,
        help="Agent folder under --root (preferred: city card validate steph from the parent).",
    ),
    root: Path = typer.Option(
        Path("."),
        "--root",
        help="Agent project, or the parent directory when an agent folder is given.",
    ),
) -> None:
    """Check that card.yaml matches the city schema."""
    try:
        project = _project_at(root) if agent is None else _agent_project(root, agent)
    except CapError as exc:
        _fail(ctx, exc)
        raise typer.Exit(code=1) from exc
    text = (project / "card.yaml").read_text(encoding="utf-8")
    try:
        card = card_from_yaml(text)
        validate_mhm_card(card)
    except CapError as exc:
        payload = {"valid": False, "code": exc.body()["code"]}
        _emit(ctx, payload, "Card rejected — fix the schema and try again", kind="bad")
        raise typer.Exit(code=1) from exc
    payload = {
        "valid": True,
        "agent_id": card["agent_id"],
        "team": card["team"],
        "role": card["role"],
    }
    _emit(
        ctx,
        payload,
        f"Card valid  [city]{card['role']}[/city]  [mute]{card['agent_id']}[/mute]",
    )


@app.command("conform")
def conform(
    ctx: typer.Context,
    profile: str = typer.Option("full", "--profile", help="Suite profile: full or fast."),
    url: str | None = typer.Option(None, "--url", help="Remote agent URL; omit for local."),
) -> None:
    """Run the conformance suite against a local or remote agent."""
    if url is not None:
        from city_conform.remote import conform_url

        async def _remote() -> dict[str, Any]:
            report = await conform_url(url, stages=select_profile(profile))
            return {
                "profile": profile,
                "url": url,
                "passed": report.passed,
                "stages": [row.as_dict() for row in report.rows],
            }

        payload = asyncio.run(_remote())
        _emit_conform(ctx, payload)
        if not payload["passed"]:
            raise typer.Exit(code=1)
        return

    from datetime import UTC, datetime

    from city_conform.runner import run_profile
    from city_conform.world import good_agent
    from city_sdk.ports import FixedClock

    async def _run() -> dict[str, Any]:
        agent = await good_agent(clock=FixedClock(datetime(2026, 11, 21, 10, 14, tzinfo=UTC)))
        report = await run_profile(agent, select_profile(profile))
        return {
            "profile": profile,
            "passed": report.passed,
            "stages": [row.as_dict() for row in report.rows],
        }

    payload = asyncio.run(_run())
    _emit_conform(ctx, payload)
    if not payload["passed"]:
        raise typer.Exit(code=1)


@app.command("dev")
def dev(
    ctx: typer.Context,
    root: Path = typer.Option(Path("."), "--root"),
    serve: bool = typer.Option(
        False,
        "--serve",
        help="Listen on loopback and accept POST /dev/invoke. The city mailbox is not contacted.",
    ),
    chat: bool = typer.Option(
        False,
        "--chat",
        help="In-process terminal chat with the local agent. The city mailbox is not contacted.",
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Loopback address to bind."),
    port: int | None = typer.Option(
        None,
        "--port",
        help="Port to bind. Default is the role port (booking_centre 8091, facility 8092).",
    ),
) -> None:
    """Run one local need, serve for curl, or chat in-process with the agent."""
    as_json = _state(ctx).get("json") is True
    if chat and serve:
        _fail(ctx, CapError("SCHEMA_INVALID", "use --chat or --serve, not both"))
        raise typer.Exit(code=1)
    if chat and as_json:
        _fail(ctx, CapError("SCHEMA_INVALID", "--chat is interactive; omit --json"))
        raise typer.Exit(code=1)
    if chat:
        try:
            chat_local(root)
        except CapError as exc:
            _fail(ctx, exc)
            raise typer.Exit(code=1) from exc
        return
    if serve:
        try:
            serve_local(root, host=host, port=port, as_json=as_json)
        except CapError as exc:
            _fail(ctx, exc)
            raise typer.Exit(code=1) from exc
        return
    payload = asyncio.run(run_local_need(root))
    _emit(
        ctx,
        payload,
        f"Asked [city]{payload['asked']}[/city]",
        kind="info",
    )


@app.command("deploy")
def deploy(
    ctx: typer.Context,
    agent: str | None = typer.Argument(
        None,
        help="Agent folder under --root (preferred: city deploy steph from the parent).",
    ),
    root: Path = typer.Option(
        Path("."),
        "--root",
        help="Parent of the agent folders when <agent> is given; otherwise the agent project.",
    ),
    promote: bool = typer.Option(
        True,
        "--promote",
        help="Release after conformance. A deploy always releases.",
    ),
    runtime: str = typer.Option("memory", "--runtime"),
) -> None:
    """Build, conform, and release an agent to the city."""
    del promote
    try:
        project = _project_at(root) if agent is None else _agent_project(root, agent)
    except CapError as exc:
        _fail(ctx, exc)
        raise typer.Exit(code=1) from exc
    text = (project / "card.yaml").read_text(encoding="utf-8")
    progress = _InlineProgress(ctx, total=_DEPLOY_STEP_TOTAL)
    progress.begin("validate card")
    try:
        preview = card_from_yaml(text)
        validate_mhm_card(preview)
    except CapError as exc:
        progress.fail()
        _fail(ctx, exc)
        raise typer.Exit(code=1) from exc
    progress.begin("package source")
    try:
        dockerfile = _read_dockerfile(project)
        files = encode_files(collect_context(project, parse_dockerfile(dockerfile)))
    except CapError as exc:
        progress.fail()
        _fail(ctx, exc)
        raise typer.Exit(code=1) from exc
    version = str(preview["version"])
    team_field = str(preview["team"])
    team = team_field.removeprefix("team:")
    store = TeamStore.open(team, root=project)
    client = _client(ctx, project, team=team)
    progress.begin("agent id")
    assigned = client.post("/agent-ids", {"family": id_family(preview)})
    _fail_http(progress, assigned)
    agent_id = str(assigned.json()["agent_id"])
    card_note: str | None = None
    if write_card_id(project / "card.yaml", agent_id):
        text = (project / "card.yaml").read_text(encoding="utf-8")
        card_note = f"Card updated  [mute]agent_id → {agent_id}[/mute]"
    progress.begin("mint agent key")
    minted = client.post(f"/agents/{agent_id}/keys", {})
    _fail_http(progress, minted)
    public = minted.json()
    progress.begin("sign grant")
    slug = id_name(agent_id)

    def signed_grant(permissions: list[str]) -> SignedGrant:
        grant = {
            "grant_id": f"dg:{team}/{slug}/{version}",
            "agent_id": agent_id,
            "version": version,
            "agent_pubkey": {
                "kty": public["kty"],
                "crv": public["crv"],
                "x": public["x"],
            },
            "permissions": permissions,
            "not_before": _stamp(ctx, hours=-1),
            "not_after": _stamp(ctx, hours=_GRANT_HOURS),
        }
        return issue(load_controller(store), grant, kid=f"team:{team}#controller")

    try:
        card = card_from_yaml(text, public)
        validate_mhm_card(card)
        permissions = grant_permissions(card)
        signed = signed_grant(permissions)
    except CapError as exc:
        progress.fail()
        _fail(ctx, exc)
        raise typer.Exit(code=1) from exc
    progress.begin("create deployment")

    def create(grant: SignedGrant) -> httpx.Response:
        return client.post(
            "/deployments",
            {
                "card": card,
                "grant": grant.grant,
                "jws": grant.jws,
                "promote": True,
                "runtime": runtime,
            },
        )

    created = create(signed)
    if permissions != ["invoke"] and _unassigned(created):
        created = create(signed_grant(["invoke"]))
    _fail_http(progress, created)
    deployment_id = str(created.json()["deployment_id"])
    uploaded = client.post(
        f"/uploads/{deployment_id}",
        {
            "dockerfile": dockerfile,
            "size_bytes": len(dockerfile.encode("utf-8")),
            "files": files,
            # Remote: return at once and poll, so no proxy timeout bounds the pipeline.
            "wait": not client.remote,
        },
    )
    if uploaded.status_code >= 400:
        progress.fail()
        _raise_for_status(uploaded)
    view = uploaded.json()
    try:
        view = _follow(ctx, client, deployment_id, view, progress=progress)
    except CapError as exc:
        progress.fail()
        _fail(ctx, exc)
        raise typer.Exit(code=1) from exc
    status_name = str(view.get("status") or "")
    outcome = view.get("outcome")
    # Deploy always promotes: done only when Index-visible and serving.
    released = status_name == "promoted" and outcome == "PASS"
    if released:
        # Local wait=True can settle without every pipeline step being observed.
        progress.catch_up(_STEP_LABELS["promote"])
        progress.ok()
    else:
        progress.fail()
    next_version: str | None = None
    if released:
        next_version = bump_card_yaml(project / "card.yaml", released=version)
        if next_version is not None:
            view = {**view, "next_version": next_version}
    wire_kind: StyleKind = "ok" if released else "bad"
    _emit(
        ctx,
        view,
        f"Status [city]{status_name}[/city]  [mute]{agent_id}@{version}[/mute]",
        kind=wire_kind,
    )
    if _state(ctx).get("json") is True:
        if not released:
            raise typer.Exit(code=1)
        return
    if card_note is not None:
        say(card_note, kind="info")
    if released:
        say(
            f"Released [city]{deployment_id}[/city]  [mute]{agent_id}@{version}[/mute]",
            kind="ok",
        )
        digest = view.get("image_digest")
        if isinstance(digest, str) and digest:
            say(f"Image  [mute]{digest}[/mute]", kind="info")
        if next_version is not None:
            say(
                f"Card bumped to [mute]{next_version}[/mute] for the next release",
                kind="info",
            )
        return
    detail = _failure_detail(view) or "Release incomplete — agent was not promoted"
    say(detail, kind="bad")
    raise typer.Exit(code=1)


_STEP_LABELS = {
    "build": "build image",
    "rollout": "roll out",
    "conformance": "conformance",
    "promote": "promote",
}
# Six local prep steps, then the four Deploy pipeline phases (build → promote).
_DEPLOY_LOCAL_STEPS = 6
_DEPLOY_STEP_TOTAL = _DEPLOY_LOCAL_STEPS + len(_STEP_LABELS)
_POLL_SECONDS = 0.5
_FOLLOW_SECONDS = 20 * 60


def _failure_detail(view: dict[str, Any]) -> str | None:
    """Prefer Deploy detail; fall back to first failing stage or outcome."""
    detail = view.get("detail")
    if isinstance(detail, str) and detail.strip():
        return detail
    stages = view.get("stages")
    if isinstance(stages, list):
        for row in stages:
            if not isinstance(row, dict):
                continue
            if row.get("outcome") not in {"FAIL", "ERROR"}:
                continue
            name = row.get("name")
            stage_detail = row.get("detail")
            if isinstance(name, str) and name:
                if isinstance(stage_detail, str) and stage_detail.strip():
                    return f"{name}: {stage_detail}"
                return name
    outcome = view.get("outcome")
    if isinstance(outcome, str) and outcome:
        return f"conformance {outcome}"
    return None


def _read_dockerfile(root: Path) -> str:
    try:
        return (root / "Dockerfile").read_text(encoding="utf-8")
    except OSError as exc:
        raise CapError("SCHEMA_INVALID", f"no Dockerfile in {root}") from exc


def _settled(view: dict[str, Any]) -> bool:
    return view.get("outcome") is not None or view.get("status") == "failed"


def _follow(
    ctx: typer.Context,
    client: _Client,
    deployment_id: str,
    view: dict[str, Any],
    *,
    progress: _InlineProgress | None = None,
) -> dict[str, Any]:
    """Poll the deployment and advance progress through pipeline phases."""

    shown: str | None = None
    deadline = time.monotonic() + _FOLLOW_SECONDS
    while True:
        if _settled(view):
            return view
        step = view.get("step")
        if isinstance(step, str) and step != shown:
            shown = step
            label = _STEP_LABELS.get(step, step)
            if progress is not None:
                progress.begin(label)
        if time.monotonic() > deadline:
            raise CapError("UPSTREAM_TIMEOUT", f"deployment {deployment_id} is still running")
        time.sleep(_POLL_SECONDS)
        try:
            response = client.get(f"/deployments/{deployment_id}")
        except httpx.HTTPError:
            continue
        if response.status_code >= 500:
            continue
        if response.status_code >= 400:
            _raise_for_status(response)
        view = response.json()


@app.command("rollback")
def rollback(
    ctx: typer.Context,
    agent: str,
    to: str | None = typer.Option(
        None,
        "--to",
        help="Version to restore; default is the previous release.",
    ),
    root: Path = typer.Option(Path("."), "--root"),
) -> None:
    """Restore a previous released image without re-running conformance."""
    try:
        agent_id, team = _resolve_agent_ref(ctx, agent, root=root)
    except CapError as exc:
        _fail(ctx, exc)
        raise typer.Exit(code=1) from exc
    body: dict[str, Any] = {}
    if to is not None:
        body["version"] = to
    response = _client(ctx, root, team=team).post(f"/agents/{agent_id}/rollback", body)
    _raise_for_status(response)
    version = response.json().get("version")
    _emit(ctx, response.json(), f"Rolled back to [city]{version}[/city]", kind="warn")


@app.command("status")
def status(
    ctx: typer.Context,
    agent: str = typer.Argument(..., help="Agent id or local folder name."),
    root: Path = typer.Option(Path("."), "--root"),
) -> None:
    """Show whether the agent is initialized, deployed, and exchanging messages."""
    try:
        agent_id, team = _resolve_agent_ref(ctx, agent, root=root)
    except CapError as exc:
        found = _undeployed(agent, root=root) if exc.code == "TARGET_NOT_VISIBLE" else None
        if found is None:
            _fail(ctx, exc)
            raise typer.Exit(code=1) from exc
        agent_id, team = found
    response = _client(ctx, root, team=team).get(f"/agents/{agent_id}/status")
    _raise_for_status(response)
    body = dict(response.json())
    local = _local_agent_project(root, agent_id) or (
        _AGENT_ID.fullmatch(agent) is None and _local_card_named(root, _ref_name(agent)) is not None
    )
    body["initialized"] = bool(body.get("initialized")) or local
    body["local_project"] = local
    if _state(ctx).get("json") is True:
        typer.echo(json.dumps(body))
        return
    _emit_status(body)


@secrets_app.command("set")
def secrets_set(
    ctx: typer.Context,
    agent: str,
    key: str,
    root: Path = typer.Option(Path("."), "--root"),
) -> None:
    try:
        agent_id, team = _resolve_agent_ref(ctx, agent, root=root)
    except CapError as exc:
        _fail(ctx, exc)
        raise typer.Exit(code=1) from exc
    value = sys.stdin.read().strip()
    if not value:
        raise typer.Exit(code=2)
    response = _client(ctx, root, team=team).post(
        f"/agents/{agent_id}/secrets",
        {"key": key, "value": value},
    )
    _raise_for_status(response)
    _emit(ctx, response.json(), f"Secret stored  [mute]{key}[/mute]")


@app.command("scale")
def scale(
    ctx: typer.Context,
    agent: str = typer.Argument(..., help="Agent id or local folder name."),
    min_replicas: int = typer.Option(..., "--min", help="Minimum pod replicas."),
    max_replicas: int = typer.Option(..., "--max", help="Maximum pod replicas."),
    concurrency: int = typer.Option(..., "--concurrency", help="Max concurrent requests per pod."),
    root: Path = typer.Option(Path("."), "--root"),
) -> None:
    """Set replica and concurrency limits for a running agent."""
    try:
        agent_id, team = _resolve_agent_ref(ctx, agent, root=root)
    except CapError as exc:
        _fail(ctx, exc)
        raise typer.Exit(code=1) from exc
    response = _client(ctx, root, team=team).post(
        f"/agents/{agent_id}/scale",
        {"min": min_replicas, "max": max_replicas, "concurrency": concurrency},
    )
    _raise_for_status(response)
    _emit(
        ctx,
        response.json(),
        f"Scale set to {min_replicas}–{max_replicas} replicas, concurrency {concurrency}",
    )


@llm_key_app.command("rotate")
def llm_rotate(
    ctx: typer.Context,
    agent: str,
    root: Path = typer.Option(Path("."), "--root"),
) -> None:
    try:
        agent_id, team = _resolve_agent_ref(ctx, agent, root=root)
    except CapError as exc:
        _fail(ctx, exc)
        raise typer.Exit(code=1) from exc
    response = _client(ctx, root, team=team).post(f"/agents/{agent_id}/llm-key/rotate", {})
    _raise_for_status(response)
    _emit(ctx, response.json(), "LLM virtual key rotated")


@app.command("compliance")
def compliance(
    ctx: typer.Context,
    agent: str = typer.Argument(..., help="Agent id or local folder name."),
    root: Path = typer.Option(Path("."), "--root"),
) -> None:
    """Fetch the city-signed build receipt for a released agent."""
    try:
        agent_id, team = _resolve_agent_ref(ctx, agent, root=root)
    except CapError as exc:
        _fail(ctx, exc)
        raise typer.Exit(code=1) from exc
    response = _client(ctx, root, team=team).get(f"/agents/{agent_id}/compliance")
    _raise_for_status(response)
    _emit(ctx, response.json(), f"Compliance receipt for [city]{agent_id}[/city]")


@app.command("delete")
def delete_agent(
    ctx: typer.Context,
    agent: str = typer.Argument(..., help="Agent name. The logged-in team owns it."),
    root: Path = typer.Option(Path("."), "--root"),
    yes: bool = typer.Option(False, "--yes", help="Skip typing the agent name."),
) -> None:
    """Remove an agent from the Index and stop its pods."""
    try:
        agent_id, team, slug = _resolve_delete_ref(ctx, agent, root=root)
    except CapError as exc:
        _fail(ctx, exc)
        raise typer.Exit(code=1) from exc
    if not yes:
        typed = typer.prompt(f"Type {slug} to delete it")
        if typed != slug:
            _fail(ctx, CapError("SCHEMA_INVALID", "typed_confirm"))
            raise typer.Exit(code=1)
    response = _client(ctx, root, team=team).delete(f"/agents/{agent_id}")
    _raise_for_status(response)
    _emit(ctx, response.json(), f"Deleted [city]{agent_id}[/city]", kind="warn")


@app.command("retire", hidden=True)
def retire(ctx: typer.Context, agent: str) -> None:
    del agent
    _fail(ctx, CapError("POLICY_VIOLATION", "city retire is gone; use city delete"))
    raise typer.Exit(code=1)


@app.command("verify")
def verify_bundle(
    ctx: typer.Context,
    bundle: Path = typer.Argument(..., help="Path to a compliance receipt bundle."),
) -> None:
    """Check a compliance receipt signature."""
    payload = json.loads(bundle.read_text(encoding="utf-8"))
    envelope = payload["receipt"]
    if not isinstance(envelope, dict):
        raise typer.Exit(code=1)
    public = PublicKey.from_jwk(payload["public_key"])
    from datetime import UTC, datetime

    verify(envelope, now=datetime.now(UTC), key_for_kid=lambda _kid: public)
    body = envelope.get("body")
    if not isinstance(body, dict):
        raise typer.Exit(code=1)
    agent_id = body.get("agent_id")
    _emit(
        ctx,
        {"verified": True, "agent_id": agent_id},
        f"Receipt verified for [city]{agent_id}[/city]",
    )


class _Client:
    def __init__(
        self,
        *,
        token: str | None,
        service_app: Any | None = None,
        base_url: str | None = None,
    ) -> None:
        if (service_app is None) == (base_url is None):
            raise CapError("POLICY_VIOLATION", "client")
        self._app = service_app
        self._base_url = base_url
        self._token = token

    @property
    def remote(self) -> bool:
        return self._base_url is not None

    def post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        return self._send("POST", path, body)

    def get(self, path: str) -> httpx.Response:
        return self._send("GET", path, None)

    def delete(self, path: str) -> httpx.Response:
        return self._send("DELETE", path, None)

    def _send(self, method: str, path: str, body: dict[str, Any] | None) -> httpx.Response:
        return asyncio.run(self._asend(method, path, body))

    async def _asend(self, method: str, path: str, body: dict[str, Any] | None) -> httpx.Response:
        headers: dict[str, str] = {}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        if self._base_url is not None:
            async with httpx.AsyncClient(base_url=self._base_url, timeout=60.0) as client:
                response = await client.request(method, path, json=body, headers=headers)
                await response.aread()
                return response
        assert self._app is not None
        transport = ASGITransport(app=self._app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://city.example",
        ) as client:
            response = await client.request(method, path, json=body, headers=headers)
            await response.aread()
            return response


def _state(ctx: typer.Context) -> dict[str, Any]:
    obj = ctx.obj
    if not isinstance(obj, dict):
        return {}
    return obj


def _emit(
    ctx: typer.Context,
    payload: Any,
    human: str,
    *,
    kind: StyleKind = "ok",
) -> None:
    if _state(ctx).get("json") is True:
        typer.echo(json.dumps(payload))
        return
    say(human, kind=kind)


class _InlineProgress:
    """One live bar + percent + step line. On a TTY each begin rewrites it."""

    def __init__(self, ctx: typer.Context, *, total: int) -> None:
        self._human = _state(ctx).get("json") is not True
        self._total = max(total, 1)
        self._index = 0
        self._name = ""
        self._started = 0.0
        self._line_mark = 0
        self._open = False

    def begin(self, name: str) -> None:
        """Start the first step or advance to the next (replaces the live line)."""

        if not self._human:
            self._index = self._index + 1 if self._open else 1
            self._name = name
            self._started = time.perf_counter()
            self._open = True
            return
        if self._open:
            prev = self._label(self._name)
            elapsed = format_duration(time.perf_counter() - self._started)
            self._index += 1
            label = self._label(name)
            if replace_status(label, kind="run", mark=self._line_mark):
                self._name = name
                self._started = time.perf_counter()
                self._line_mark = line_mark()
                return
            say(f"{prev}  [mute]{elapsed}[/mute]", kind="ok")
            self._name = name
            self._started = time.perf_counter()
            say(label, kind="run")
            self._line_mark = line_mark()
            return
        self._index = 1
        self._name = name
        self._started = time.perf_counter()
        self._open = True
        say(self._label(name), kind="run")
        self._line_mark = line_mark()

    def catch_up(self, name: str) -> None:
        """Jump to the final step when the pipeline settled without every phase."""

        if not self._open or self._index >= self._total:
            return
        if not self._human:
            self._index = self._total
            self._name = name
            self._started = time.perf_counter()
            return
        prev = self._label(self._name)
        elapsed = format_duration(time.perf_counter() - self._started)
        self._index = self._total
        self._name = name
        self._started = time.perf_counter()
        label = self._label(name)
        if replace_status(label, kind="run", mark=self._line_mark):
            self._line_mark = line_mark()
            return
        say(f"{prev}  [mute]{elapsed}[/mute]", kind="ok")
        say(label, kind="run")
        self._line_mark = line_mark()

    def ok(self) -> None:
        self._seal("ok")

    def fail(self) -> None:
        self._seal("bad")

    def _percent(self, *, done: bool = False) -> int:
        if done:
            return 100
        return min(100, int(100 * self._index / self._total))

    def _bar(self, *, done: bool = False) -> str:
        filled = self._total if done else max(0, min(self._total, self._index))
        return "█" * filled + "░" * (self._total - filled)

    def _label(self, name: str, *, done: bool = False) -> str:
        bar = self._bar(done=done)
        pct = self._percent(done=done)
        return f"[{bar}] {pct}%  [{self._index}/{self._total}] {name}"

    def _seal(self, kind: StyleKind) -> None:
        if not self._human or not self._open:
            self._open = False
            return
        elapsed = format_duration(time.perf_counter() - self._started)
        line = f"{self._label(self._name, done=kind == 'ok')}  [mute]{elapsed}[/mute]"
        if replace_status(line, kind=kind, mark=self._line_mark):
            self._open = False
            return
        say(line, kind=kind)
        self._open = False


def _fail_http(progress: _InlineProgress, response: httpx.Response) -> None:
    if response.status_code >= 400:
        progress.fail()
        _raise_for_status(response)


def _emit_failing_stages(stages: object) -> None:
    """Print each FAIL/ERROR stage with its detail (human mode)."""

    if not isinstance(stages, list):
        return
    for row in stages:
        if not isinstance(row, dict):
            continue
        if row.get("outcome") not in {"FAIL", "ERROR"}:
            continue
        name = row.get("name")
        stage_name = name if isinstance(name, str) and name else "stage"
        stage_detail = row.get("detail")
        if isinstance(stage_detail, str) and stage_detail.strip():
            say(f"{stage_name}: {stage_detail}", kind="bad")
        else:
            say(stage_name, kind="bad")


def _emit_conform(ctx: typer.Context, payload: dict[str, Any]) -> None:
    if payload["passed"]:
        _emit(ctx, payload, "Conformance passed", kind="pass")
        return
    if _state(ctx).get("json") is True:
        typer.echo(json.dumps(payload))
        return
    say("Conformance failed", kind="fail")
    _emit_failing_stages(payload.get("stages"))


def _emit_status(body: Mapping[str, Any]) -> None:
    """Human checklist: initialized → deployed → messaged."""

    agent_id = str(body.get("agent_id") or "")
    initialized = bool(body.get("initialized"))
    deployed = bool(body.get("deployed"))
    messaged = bool(body.get("messaged"))
    raw_count = body.get("message_count")
    message_count = raw_count if isinstance(raw_count, int) and raw_count >= 0 else 0
    visibility = body.get("visibility")
    version = body.get("version")
    local = bool(body.get("local_project"))

    if initialized and local:
        init_value = "[ok]Yes[/ok]  local project ready"
    elif initialized:
        init_value = "[ok]Yes[/ok]  keys registered on the city"
    else:
        init_value = "[bad]No[/bad]  run [mute]city init agent[/mute]"

    if deployed:
        deploy_value = f"[ok]Yes[/ok]  [city]{visibility}[/city]  [mute]{version}[/mute]"
    elif visibility is not None:
        deploy_value = f"[warn]No[/warn]  [city]{visibility}[/city]"
    else:
        deploy_value = "[bad]No[/bad]  not listed in the Index"

    unit = "message" if message_count == 1 else "messages"
    if messaged and message_count > 0:
        message_value = f"[ok]Yes[/ok]  {message_count} {unit} exchanged"
    elif messaged:
        message_value = "[ok]Yes[/ok]  at least one exchange"
    else:
        message_value = f"[bad]No[/bad]  {message_count} {unit} exchanged"

    report(
        agent_id,
        (
            ("Initialized", init_value),
            ("Deployed", deploy_value),
            ("Messaged", message_value),
        ),
        subtitle="Agent readiness",
    )


def _local_agent_project(root: Path, agent_id: str) -> bool:
    """True when ``card.yaml`` for this agent exists under ``--root``."""

    candidates = (root / "card.yaml", *sorted(root.glob("*/card.yaml")))
    for path in candidates:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        found = flat_field(text, "agent_id")
        if found is None:
            try:
                found = unminted_id(load_card_yaml(text))
            except CapError:
                continue
        if found == agent_id:
            return True
    return False


def _fail(ctx: typer.Context, exc: CapError) -> None:
    body = exc.body()
    if _state(ctx).get("json") is True:
        typer.echo(json.dumps(body))
        return
    style_fail(body["code"], body.get("message", ""))


def _client(
    ctx: typer.Context,
    root: Path,
    token: str | None = "",
    *,
    team: str | None = None,
) -> _Client:
    state = _state(ctx)
    chosen = token
    if chosen == "":
        store = _store_for(root, team)
        chosen = store.read_session()
    # Injected app (tests) wins over the default remote platform URL.
    service_app = state.get("app")
    if service_app is not None:
        return _Client(token=chosen, service_app=service_app)
    api_url = state.get("api_url")
    if isinstance(api_url, str) and api_url:
        return _Client(token=chosen, base_url=api_url)
    # One in-process DeployService for the whole CLI invocation. Creating a
    # new app per call drops device-login sessions before /keys/register.
    service_app = create_app(DeployService())
    state["app"] = service_app
    return _Client(token=chosen, service_app=service_app)


def _store_for(root: Path, team: str | None) -> TeamStore:
    if team:
        return TeamStore.open(team, root=root)
    return TeamStore.open(_team_from_root(root), root=root)


def _project_at(root: Path) -> Path:
    """The project itself, when ``city deploy`` / ``city card validate`` runs inside it."""

    project = root.resolve()
    if (project / "card.yaml").is_file():
        return project
    raise CapError(
        "SCHEMA_INVALID",
        "pass an agent folder from the parent: city deploy <name>",
    )


def _agent_project(root: Path, agent: str) -> Path:
    """Resolve ``<root>/<agent>/`` as an agent project (must contain ``card.yaml``)."""
    if re.fullmatch(r"[a-z0-9_-]+", agent) is None:
        raise CapError("SCHEMA_INVALID", "agent")
    project = (root / agent).resolve()
    if not (project / "card.yaml").is_file():
        raise CapError("SCHEMA_INVALID", f"no agent project at {project.name}/")
    return project


def _team_from_root(root: Path) -> str:
    text = (root / "card.yaml").read_text(encoding="utf-8")
    found = flat_field(text, "team")
    if found is None:
        raise CapError("SCHEMA_INVALID", "team")
    return found.removeprefix("team:")


_AGENT_SLUG = re.compile(r"^[a-z0-9_-]+$")
_AGENT_ID = re.compile(r"^agent:[a-z0-9_-]+/[a-z0-9_-]+$|^agent:[0-9a-hjkmnp-tv-z]{12}$")


def _resolve_delete_ref(ctx: typer.Context, agent: str, *, root: Path) -> tuple[str, str, str]:
    """``(agent_id, team_id, name to type)`` for a delete. Deploy checks the owner.

    A delete acts for the logged-in team; a local folder of another team is not followed.
    """

    team = resolve_active_team(root=root)
    if _AGENT_ID.fullmatch(agent) is not None:
        return agent, team, agent
    name = _ref_name(agent)
    local = _local_card_named(root, name)
    family = name
    if local is not None and _session_team(local, root=root) == team:
        found = local.get("agent_id")
        if isinstance(found, str) and _AGENT_ID.fullmatch(found) and _minted(found):
            return found, team, name
        family = _card_family(local, name)
    return _issued_id(ctx, root, team, family, name), team, name


def _resolve_agent_ref(ctx: typer.Context, agent: str, *, root: Path) -> tuple[str, str]:
    """Expand a CLI agent ref to ``(agent_id, team_id)``.

    Accepts the full city id (``agent:<rid>``), an agent folder whose ``card.yaml``
    holds the id, or a role family (``gp``, ``hospital``, …) looked up among the
    team's ids. The id carries no team (ADR-085): the team is the local card's when
    it still has a login session, otherwise the active ``city login`` team.
    """
    if _AGENT_ID.fullmatch(agent) is not None:
        return agent, _team_for_id(agent, root=root)
    name = _ref_name(agent)
    family = name
    local = _local_card_named(root, name)
    team = resolve_active_team(root=root)
    if local is not None:
        team = _session_team(local, root=root) or team
        found = local.get("agent_id")
        if isinstance(found, str) and _AGENT_ID.fullmatch(found) and _minted(found):
            return found, team
        family = _card_family(local, name)
    return _issued_id(ctx, root, team, family, name), team


def _undeployed(agent: str, *, root: Path) -> tuple[str, str] | None:
    """A local scaffold that was never deployed: its placeholder id and its team."""

    if _AGENT_ID.fullmatch(agent) is not None:
        return None
    local = _local_card_named(root, _ref_name(agent))
    if local is None:
        return None
    found = local.get("agent_id", unminted_id(local))
    if not isinstance(found, str) or _minted(found):
        return None
    return found, _session_team(local, root=root) or resolve_active_team(root=root)


def _ref_name(agent: str) -> str:
    name = agent.removeprefix("agent:")
    if _AGENT_SLUG.fullmatch(name) is None:
        raise CapError("SCHEMA_INVALID", "agent")
    return name


def _local_card_named(root: Path, name: str) -> dict[str, Any] | None:
    """``<root>/<name>/card.yaml``, or ``<root>/card.yaml`` when ``--root`` is that folder."""

    nested = _local_card(root / name / "card.yaml")
    if nested is not None:
        return nested
    here = _local_card(root / "card.yaml")
    if here is not None and name in (root.resolve().name, here.get("name")):
        return here
    return None


def _card_family(card: Mapping[str, Any], name: str) -> str:
    try:
        return id_family(card)
    except CapError:
        return name


def _issued_id(ctx: typer.Context, root: Path, team: str, family: str, name: str) -> str:
    """The id Deploy issued to ``team`` for ``family``."""

    if family not in ID_FAMILIES:
        raise CapError("TARGET_NOT_VISIBLE", f"no agent named {name}")
    response = _client(ctx, root, team=team).get("/agent-ids")
    _raise_for_status(response)
    for row in response.json().get("agent_ids", []):
        if isinstance(row, dict) and row.get("family") == family:
            return str(row["agent_id"])
    raise CapError("TARGET_NOT_VISIBLE", f"the team has no {family} agent")


def _minted(agent_id: str) -> bool:
    return not agent_id.startswith(f"agent:{UNMINTED}/")


def _local_card(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return load_card_yaml(path.read_text(encoding="utf-8"))
    except (OSError, CapError, ValueError):
        return None


def _session_team(card: Mapping[str, Any], *, root: Path) -> str | None:
    found = card.get("team")
    if not isinstance(found, str):
        return None
    team = found.removeprefix("team:")
    if _AGENT_SLUG.fullmatch(team) is None:
        return None
    return team if TeamStore.open(team, root=root).read_session() else None


def _team_for_id(agent_id: str, *, root: Path) -> str:
    """The team of the local card that holds ``agent_id``, else the active login."""

    for path in (root / "card.yaml", *sorted(root.glob("*/card.yaml"))):
        card = _local_card(path)
        if card is not None and card.get("agent_id") == agent_id:
            team = _session_team(card, root=root)
            if team is not None:
                return team
    return resolve_active_team(root=root)


def grant_permissions(card: Mapping[str, Any]) -> list[str]:
    """The deploy grant signs the card's role permissions (ADR-044), or ``invoke``."""

    family = family_of(card)
    return ["invoke"] if family is None else sorted(role_permissions(family))


def _unassigned(response: httpx.Response) -> bool:
    """Deploy refused the role grant because the team has no ASST: sign ``invoke``."""

    if response.status_code != 403:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    return (
        isinstance(body, dict)
        and body.get("code") == "GRANT_PERMISSION_DENIED"
        and body.get("message") == UNASSIGNED
    )


def _stamp(ctx: typer.Context, *, hours: int) -> str:
    state = _state(ctx)
    if state.get("app") is not None:
        return format_time(_service(ctx).now() + timedelta(hours=hours))
    api_url = state.get("api_url")
    if isinstance(api_url, str) and api_url:
        return format_time(datetime.now(UTC) + timedelta(hours=hours))
    return format_time(_service(ctx).now() + timedelta(hours=hours))


def _service(ctx: typer.Context) -> DeployService:
    state = _state(ctx)
    service_app = state.get("app")
    if service_app is None:
        if isinstance(state.get("api_url"), str) and state.get("api_url"):
            raise CapError("POLICY_VIOLATION", "remote")
        raise CapError("POLICY_VIOLATION", "app")
    deploy = service_app.state.deploy
    if not isinstance(deploy, DeployService):
        raise CapError("POLICY_VIOLATION", "app")
    return deploy


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code >= 400:
        try:
            body = response.json()
        except ValueError:
            style_fail("HTTP", response.text.strip() or str(response.status_code))
            raise typer.Exit(code=1)
        if isinstance(body, dict) and isinstance(body.get("code"), str):
            detail = body.get("message")
            message = detail if isinstance(detail, str) else ""
            # Deploy dropped or never saw this bearer (restart, wrong team file, stale login).
            if body["code"] == "POLICY_VIOLATION" and message == "unknown team":
                message = "Unknown team — run city login again"
            style_fail(body["code"], message)
        else:
            style_fail("HTTP", response.text.strip() or str(response.status_code))
        raise typer.Exit(code=1)
