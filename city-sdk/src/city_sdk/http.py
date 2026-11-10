"""HTTP adapters for the Index and the Relay mailbox. Memory stays the harness default."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx
from cap.core.envelope import build, sign
from cap.core.errors import HTTP_STATUS, CapError
from cap.core.keys import PrivateKey, PublicKey

from city_sdk.city import Message, Peer
from city_sdk.ports import Clock, SystemClock

_LOG = logging.getLogger("city.sdk.http")
_PROFILE = "mhm/1.0"
_KEY_TTL_S = 60.0
_KEY_TRIES = 3
_KEY_RETRY_S = 0.4
_HISTORY_LIMIT = 200
_HISTORY_PAGES = 5
# Deploy conformance mints this sender; it is never published to the Index.
_CONFORM_STRANGER = "agent:t99/stranger#"


class HttpIndex:
    def __init__(self, *, caller: str, client: httpx.AsyncClient) -> None:
        self.caller = caller
        self._client = client

    async def search(self, query: str) -> list[Peer]:
        """``POST /v1/agents/search``: the agents of the families the text names."""

        _LOG.info("index search", extra={"caller": self.caller})
        response = await self._client.post(
            "/v1/agents/search",
            json={"query": query},
            headers={"X-Caller": self.caller},
        )
        if response.status_code >= 400:
            raise _cap_http(response, "index search")
        body = response.json()
        rows = body.get("agents") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise CapError("SCHEMA_INVALID", "index search")
        found: list[Peer] = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("agent_id"), str):
                continue
            role = row.get("role")
            found.append(
                Peer(
                    str(row["agent_id"]),
                    role=role if isinstance(role, str) else "",
                    reputation=_number(row.get("reputation")),
                    service_types=_strings(row.get("service_types")),
                    no_clinical_advice=row.get("no_clinical_advice") is True,
                    profiles=_strings(row.get("profiles")),
                )
            )
        families = body.get("families") if isinstance(body, dict) else None
        _LOG.info(
            "index found",
            extra={"count": len(found), "families": _strings(families)},
        )
        return found


def _number(value: object) -> int | float:
    return value if isinstance(value, int | float) and not isinstance(value, bool) else 0


class HttpRelay:
    """Signs outbound needs. The brain still only sees ``City``."""

    def __init__(
        self,
        *,
        agent_id: str,
        private: PrivateKey,
        client: httpx.AsyncClient,
        grant_body: Mapping[str, Any],
        kid: str | None = None,
        clock: Clock | None = None,
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
        self.renewed: list[tuple[str, int]] = []
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
        return msg_id

    async def history(self, *, peer: str | None, need_id: str | None) -> list[Message]:
        """This agent's own messages, oldest first, read page by page from the newest
        (``readInboxHistory``). Each page is a signed ``inbox.history`` envelope."""

        pages: list[list[Message]] = []
        cursor: str | None = None
        for _ in range(_HISTORY_PAGES):
            query: dict[str, object] = {"limit": _HISTORY_LIMIT}
            if peer is not None:
                query["peer"] = peer
            if need_id is not None:
                query["need_id"] = need_id
            if cursor is not None:
                query["cursor"] = cursor
            envelope = sign(
                build(
                    msg_id=str(uuid4()),
                    message_type="inbox.history",
                    sender=self.agent_id,
                    recipient="city:relay",
                    sent_at=self.clock.now(),
                    body=query,
                    profile=_PROFILE,
                ),
                self._private,
                kid=self.kid,
                grant_id=self.grant_id,
            )
            response = await self._client.post(
                f"/v1/markets/{self.market_id}/inbox/history", json=envelope
            )
            if response.status_code >= 400:
                raise _cap_http(response, "relay history")
            body = response.json()
            rows = body.get("messages") if isinstance(body, dict) else None
            if not isinstance(rows, list):
                raise CapError("SCHEMA_INVALID", "relay history")
            pages.append([_history_message(row) for row in rows if isinstance(row, dict)])
            raw = body.get("next_cursor")
            if not isinstance(raw, str) or not raw:
                break
            cursor = raw
        _LOG.info(
            "relay history",
            extra={"agent_id": self.agent_id, "need_id": need_id, "pages": len(pages)},
        )
        return [row for page in reversed(pages) for row in page]

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


def _strings(value: object) -> tuple[str, ...]:
    return tuple(t for t in value if isinstance(t, str)) if isinstance(value, list) else ()


def _history_message(row: Mapping[str, Any]) -> Message:
    def text(key: str) -> str | None:
        value = row.get(key)
        return value if isinstance(value, str) else None

    body: dict[str, object] = {"text": text("text") or ""}
    status = text("status")
    if status is not None:
        body["status"] = status
    return Message(
        type=text("type") or "chat",
        sender=text("sender") or "",
        recipient=text("recipient") or "",
        msg_id=text("msg_id") or "",
        need_id=text("need_id"),
        conversation_id=text("conversation_id"),
        body=body,
        sent_at=text("sent_at") or "",
    )
