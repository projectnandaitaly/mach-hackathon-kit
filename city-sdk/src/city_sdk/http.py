"""HTTP adapters for Index, Relay, and Ledger. Memory stays the harness default."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping
from datetime import date
from typing import Any
from urllib.parse import quote, urlencode
from uuid import uuid4

import httpx
from cap.core.envelope import build, sign
from cap.core.errors import HTTP_STATUS, CapError
from cap.core.keys import PrivateKey, PublicKey

from city_sdk.market import Peer
from city_sdk.ports import Clock, SystemClock

_LOG = logging.getLogger("city.sdk.http")
_PROFILE = "mhm/1.0"
_KEY_TTL_S = 60.0
_KEY_TRIES = 3
_KEY_RETRY_S = 0.4
# Deploy conformance mints this sender; it is never published to the Index.
_CONFORM_STRANGER = "agent:t99/stranger#"


class HttpIndex:
    def __init__(self, *, caller: str, client: httpx.AsyncClient) -> None:
        self.caller = caller
        self._client = client

    async def find(self, *, role: str) -> list[Peer]:
        _LOG.info("index find", extra={"role": role, "caller": self.caller})
        response = await self._client.get(
            "/v1/agents",
            params={"role": role},
            headers={"X-Caller": self.caller},
        )
        if response.status_code >= 400:
            raise _cap_http(response, "index query")
        body = response.json()
        rows = body.get("agents") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise CapError("SCHEMA_INVALID", "index query")
        found: list[Peer] = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("agent_id"), str):
                continue
            services, profiles = _strings(row.get("service_types")), _strings(row.get("profiles"))
            found.append(
                Peer(
                    str(row["agent_id"]),
                    role=role,
                    reputation=_number(row.get("reputation")),
                    service_types=services,
                    no_clinical_advice=row.get("no_clinical_advice") is True,
                    profiles=profiles,
                )
            )
        _LOG.info("index found", extra={"count": len(found), "role": role})
        return found


def _number(value: object) -> int | float:
    return value if isinstance(value, int | float) and not isinstance(value, bool) else 0


class HttpEvidence:
    """POST escalate/sent effects to Attestation ``/v1/evidence``."""

    def __init__(
        self,
        *,
        agent_id: str,
        client: httpx.AsyncClient,
        grant_body: Mapping[str, Any],
    ) -> None:
        self.agent_id = agent_id
        self._client = client
        self._grant = json.dumps(dict(grant_body), separators=(",", ":"))

    async def escalate(self, reason: str) -> None:
        await self._post({"kind": "escalate", "reason": reason})

    async def sent(
        self,
        peer: str,
        body: dict[str, object],
        *,
        traceparent: str | None,
    ) -> None:
        await self._post(
            {
                "kind": "sent",
                "peer": peer,
                "body": dict(body),
                "traceparent": traceparent,
            }
        )

    async def _post(self, payload: dict[str, object]) -> None:
        body = {"agent_id": self.agent_id, **payload}
        response = await self._client.post(
            "/v1/evidence",
            json=body,
            headers={"X-City-Grant": self._grant},
        )
        if response.status_code >= 400:
            raise _cap_http(response, "evidence")


class HttpRelay:
    """Signs outbound needs. The brain still only sees ``Market``."""

    def __init__(
        self,
        *,
        agent_id: str,
        private: PrivateKey,
        client: httpx.AsyncClient,
        grant_body: Mapping[str, Any],
        kid: str | None = None,
        clock: Clock | None = None,
        evidence: HttpEvidence | None = None,
    ) -> None:
        grant = grant_body.get("grant")
        if not isinstance(grant, Mapping) or not isinstance(grant.get("grant_id"), str):
            raise CapError("SCHEMA_INVALID", "grant")
        self.agent_id = agent_id
        self._private = private
        self._client = client
        self._grant_body = dict(grant_body)
        self.grant_id = str(grant["grant_id"])
        self.kid = agent_id + "#k1" if kid is None else kid
        self.clock: Clock = clock if clock is not None else SystemClock()
        self.market_id = "m-1"
        self.acks: list[tuple[str, int, str]] = []
        self.sent: list[tuple[str, dict[str, object], str | None]] = []
        self.renewed: list[tuple[str, int]] = []
        self._evidence = evidence
        self.joined: list[str] = []

    async def join(self, market_id: str) -> dict[str, object]:
        self.market_id = market_id
        path = f"/v1/markets/{market_id}/join"
        _LOG.info(
            "relay join",
            extra={"agent_id": self.agent_id, "market_id": market_id, "grant_id": self.grant_id},
        )
        response = await self._client.post(path, json=self._grant_body)
        if response.status_code >= 400:
            raise _cap_http(response, "relay join")
        self.joined.append(market_id)
        body = response.json()
        if isinstance(body, dict):
            session = {str(key): value for key, value in body.items()}
            _LOG.info(
                "relay joined",
                extra={
                    "agent_id": self.agent_id,
                    "market_id": market_id,
                    "session_id": session.get("session_id"),
                },
            )
            return session
        return {}

    async def send(
        self,
        agent_id: str,
        body: dict[str, object],
        *,
        traceparent: str | None,
    ) -> None:
        self.sent.append((agent_id, dict(body), traceparent))
        msg_id = str(uuid4())
        envelope = sign(
            build(
                msg_id=msg_id,
                message_type="chat" if set(body) == {"text"} else "need.request",
                sender=self.agent_id,
                recipient=agent_id,
                sent_at=self.clock.now(),
                body=body,
                traceparent=traceparent,
            ),
            self._private,
            kid=self.kid,
            grant_id=self.grant_id,
        )
        _LOG.info(
            "relay send",
            extra={"agent_id": self.agent_id, "to": agent_id, "msg_id": msg_id},
        )
        response = await self._client.post(f"/v1/markets/{self.market_id}/messages", json=envelope)
        if response.status_code >= 400:
            raise _cap_http(response, "relay send")
        if self._evidence is not None:
            await self._evidence.sent(agent_id, dict(body), traceparent=traceparent)

    async def post(
        self,
        *,
        to: str,
        message_type: str,
        body: dict[str, object],
        need_id: str | None,
        conversation_id: str | None,
        traceparent: str | None,
    ) -> str:
        msg_id = str(uuid4())
        envelope = sign(
            build(
                msg_id=msg_id,
                message_type=message_type,
                sender=self.agent_id,
                recipient=to,
                sent_at=self.clock.now(),
                body=body,
                profile=_PROFILE,
                need_id=need_id,
                conversation_id=conversation_id,
                traceparent=traceparent,
            ),
            self._private,
            kid=self.kid,
            grant_id=self.grant_id,
        )
        _LOG.info(
            "relay post",
            extra={
                "agent_id": self.agent_id,
                "to": to,
                "msg_id": msg_id,
                "need_id": need_id,
                "type": message_type,
            },
        )
        response = await self._client.post(f"/v1/markets/{self.market_id}/messages", json=envelope)
        if response.status_code >= 400:
            raise _cap_http(response, "relay send")
        if self._evidence is not None:
            await self._evidence.sent(to, dict(body), traceparent=traceparent)
        return msg_id

    async def ack(self, *, msg_id: str, fence: int, status: str) -> None:
        self.acks.append((msg_id, fence, status))
        _LOG.info(
            "relay ack",
            extra={"agent_id": self.agent_id, "msg_id": msg_id, "fence": fence, "status": status},
        )
        response = await self._client.post(
            f"/v1/markets/{self.market_id}/inbox/ack",
            json={"msg_id": msg_id, "fence": fence, "ack": {"status": status}},
        )
        if response.status_code >= 400:
            raise _cap_http(response, "relay ack")

    async def renew(self, *, msg_id: str, fence: int) -> None:
        self.renewed.append((msg_id, fence))
        _LOG.info(
            "relay renew",
            extra={"agent_id": self.agent_id, "msg_id": msg_id, "fence": fence},
        )
        response = await self._client.post(
            f"/v1/markets/{self.market_id}/inbox/renew",
            json={"msg_id": msg_id, "fence": fence},
        )
        if response.status_code >= 400:
            raise _cap_http(response, "relay renew")


def _cap_http(response: httpx.Response, fallback: str) -> CapError:
    """Map a city HTTP error body to CapError. Never logs the response body."""

    code = "POLICY_VIOLATION"
    message = fallback
    try:
        payload: Any = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        raw_code = payload.get("code")
        raw_message = payload.get("message")
        if isinstance(raw_code, str) and raw_code in HTTP_STATUS:
            code = raw_code
        if isinstance(raw_message, str) and raw_message:
            message = raw_message
    _LOG.warning(
        "city http failed",
        extra={
            "path": response.request.url.path,
            "status": response.status_code,
            "code": code,
            "detail": message,
        },
    )
    return CapError(code, message)


class HttpLedger:
    """Facility capacity calls. The grant header is not logged."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        grant_body: Mapping[str, Any],
        facility_id: str,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self._client = client
        self._grant = json.dumps(dict(grant_body), separators=(",", ":"))
        self._facility = facility_id
        self._fence: dict[str, int] = {}

    async def hold(self, agent_id: str) -> str:
        self.calls.append(("hold", agent_id))
        response = await self._client.post(
            f"/v1/facilities/{quote(self._facility, safe='')}/holds",
            headers=self._headers(),
            json={"offer_id": agent_id, "fence": 1, "ttl_seconds": 120},
        )
        if response.status_code >= 400:
            raise _cap_http(response, "ledger")
        body = response.json()
        hold_id = body.get("hold_id") if isinstance(body, dict) else None
        if not isinstance(hold_id, str) or not hold_id:
            raise CapError("SCHEMA_INVALID", "ledger")
        fence = body.get("fence") if isinstance(body, dict) else None
        self._fence[hold_id] = fence if isinstance(fence, int) else 1
        return hold_id

    async def commit(self, hold_id: str) -> None:
        self.calls.append(("commit", hold_id))
        response = await self._client.post(
            f"/v1/holds/{quote(hold_id, safe='')}/commit",
            headers=self._headers(),
            json={"fence": self._fence.get(hold_id, 1), "consent_ref": hold_id},
        )
        if response.status_code >= 400:
            raise _cap_http(response, "ledger")

    async def release(self, hold_id: str) -> None:
        self.calls.append(("release", hold_id))
        response = await self._client.post(
            f"/v1/holds/{quote(hold_id, safe='')}/cancel",
            headers=self._headers(),
        )
        if response.status_code >= 400:
            raise _cap_http(response, "ledger")

    def _headers(self) -> dict[str, str]:
        return {"X-City-Grant": self._grant}


class HttpCare:
    """Ledger care registers and the wave clock. The grant header is not logged."""

    def __init__(self, *, client: httpx.AsyncClient, grant_body: Mapping[str, Any]) -> None:
        self._client = client
        self._grant = json.dumps(dict(grant_body), separators=(",", ":"))

    async def world(
        self, from_date: date | None = None, until_date: date | None = None
    ) -> dict[str, Any]:
        """ADR-082: the slots of ``[from_date, until_date)``; the Ledger's default window
        (today plus 14 agenda days) when both are absent."""

        query = {
            name: value.isoformat()
            for name, value in (("from", from_date), ("until", until_date))
            if value is not None
        }
        path = "/v1/world" + ("?" + urlencode(query) if query else "")
        return await self._call("GET", path, route="/v1/world")

    async def agenda_now(self) -> dict[str, Any]:
        return await self._call("GET", "/v1/agenda/now")

    async def rules(self) -> list[dict[str, Any]]:
        rows = (await self._call("GET", "/v1/rules")).get("rules")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise CapError("SCHEMA_INVALID", "ledger rules")
        return rows

    async def issue_prescription(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._call("POST", "/v1/prescriptions", body)

    async def prescription(self, prescription_number: str) -> dict[str, Any]:
        path = f"/v1/prescriptions/{quote(prescription_number, safe='')}"
        return await self._call("GET", path, route="/v1/prescriptions/{prescription_number}")

    async def checkin(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._call("POST", "/v1/checkins", body)

    async def report(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._call("POST", "/v1/reports", body)

    async def hold_slot(self, facility_id: str, body: dict[str, Any]) -> dict[str, Any]:
        path = f"/v1/facilities/{quote(facility_id, safe='')}/holds"
        need_id = body.get("need_id")
        return await self._call(
            "POST",
            path,
            body,
            route="/v1/facilities/{facility_id}/holds",
            need_id=need_id if isinstance(need_id, str) else None,
        )

    async def commit_slot(self, hold_id: str, body: dict[str, Any]) -> dict[str, Any]:
        path = f"/v1/holds/{quote(hold_id, safe='')}/commit"
        return await self._call("POST", path, body, route="/v1/holds/{hold_id}/commit")

    async def put_note(self, key: str, value: str, *, need_id: str | None) -> None:
        body: dict[str, Any] = {"value": value}
        if need_id is not None:
            body["need_id"] = need_id
        path = f"/v1/memory/{quote(key, safe='')}"
        await self._call("PUT", path, body, route="/v1/memory/{key}", need_id=need_id)

    async def get_note(self, key: str) -> str | None:
        try:
            note = await self._call(
                "GET", f"/v1/memory/{quote(key, safe='')}", route="/v1/memory/{key}"
            )
        except CapError as exc:
            if exc.code == "TARGET_NOT_VISIBLE":
                return None
            raise
        value = note.get("value")
        if not isinstance(value, str):
            raise CapError("SCHEMA_INVALID", "ledger note")
        return value

    async def history(self, need_id: str) -> dict[str, Any]:
        path = f"/v1/history/{quote(need_id, safe='')}"
        return await self._call("GET", path, route="/v1/history/{need_id}", need_id=need_id)

    async def _call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        route: str | None = None,
        need_id: str | None = None,
    ) -> dict[str, Any]:
        extra: dict[str, object] = {"method": method, "route": route or path}
        if need_id is not None:
            extra["need_id"] = need_id
        _LOG.info("ledger care", extra=extra)
        response = await self._client.request(
            method, path, json=body, headers={"X-City-Grant": self._grant}
        )
        if response.status_code >= 400:
            raise _cap_http(response, "ledger")
        answer = response.json()
        if not isinstance(answer, dict):
            raise CapError("SCHEMA_INVALID", "ledger")
        return answer


class HttpKeys:
    """Resolve a kid at the Index. Hits are cached; misses are asked again."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        ttl_s: float = _KEY_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._ttl = ttl_s
        self._clock = clock
        self._cache: dict[str, tuple[float, PublicKey]] = {}

    async def __call__(self, kid: str) -> PublicKey | None:
        """Return the public key, or None when Index says the kid is unknown (404).

        A transport stall or Index 5xx must not become ``KEY_UNKNOWN`` (401): that
        rejects legitimate citizens under load. Raise ``UPSTREAM_TIMEOUT`` (504) so
        the Relay can retry. Only a clean 404 is unknown — that is what
        ``invoke.signature`` expects for the stranger kid.

        The conformance stranger is never in the Index: skip the round-trip so a
        busy Index cannot turn that stage into a 504.
        """

        if kid.startswith(_CONFORM_STRANGER):
            return None
        cached = self._cache.get(kid)
        if cached is not None and self._clock() < cached[0]:
            return cached[1]
        last: BaseException | None = None
        for attempt in range(_KEY_TRIES):
            try:
                response = await self._client.get(f"/v1/keys/{quote(kid, safe='')}")
            except httpx.HTTPError as exc:
                last = exc
                if attempt + 1 < _KEY_TRIES:
                    await asyncio.sleep(_KEY_RETRY_S)
                    continue
                raise CapError("UPSTREAM_TIMEOUT", "index key") from exc
            if response.status_code == 404:
                return None
            if response.status_code >= 500:
                last = CapError("UPSTREAM_TIMEOUT", "index key")
                if attempt + 1 < _KEY_TRIES:
                    await asyncio.sleep(_KEY_RETRY_S)
                    continue
                raise CapError("UPSTREAM_TIMEOUT", "index key")
            if response.status_code >= 400:
                raise _cap_http(response, "index key")
            public = PublicKey.from_jwk(response.json())
            self._cache[kid] = (self._clock() + self._ttl, public)
            return public
        raise CapError("UPSTREAM_TIMEOUT", "index key") from last


class ClosedLedger:
    """Fail closed. A cloud process does not pretend the memory ledger is durable."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def hold(self, agent_id: str) -> str:
        del agent_id
        raise CapError("POLICY_VIOLATION", "ledger")

    async def commit(self, hold_id: str) -> None:
        del hold_id
        raise CapError("POLICY_VIOLATION", "ledger")

    async def release(self, hold_id: str) -> None:
        del hold_id
        raise CapError("POLICY_VIOLATION", "ledger")


class ClosedAttest:
    def __init__(self) -> None:
        self.ratings: list[tuple[str, int]] = []

    async def rate(self, agent_id: str, score: int) -> None:
        del agent_id, score
        raise CapError("POLICY_VIOLATION", "attestation")


def _strings(value: object) -> tuple[str, ...]:
    return tuple(t for t in value if isinstance(t, str)) if isinstance(value, list) else ()
