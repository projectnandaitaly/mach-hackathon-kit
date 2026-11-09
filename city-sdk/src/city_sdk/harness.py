"""Delivery, journal, probe, and shutdown. The brain never sees the fence."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from cap.core.envelope import build, sign, verify
from cap.core.errors import CapError
from cap.core.jcs import card_digest, digest
from cap.core.keys import PrivateKey, PublicKey

from city_sdk.journal import JournalEntry, MemoryJournal
from city_sdk.layers import LayerRegistry
from city_sdk.llm import LlmConnector
from city_sdk.logs import HarnessLog
from city_sdk.market import (
    AttestPort,
    CarePort,
    EvidencePort,
    IndexPort,
    Market,
    Message,
    Need,
    NotesPort,
    OutOfScope,
)
from city_sdk.policy import PolicyStore
from city_sdk.ports import (
    Clock,
    HarnessLedger,
    HarnessRelay,
    MemoryAttest,
    MemoryCare,
    MemoryIndex,
    MemoryLedger,
    MemoryRelay,
    SystemClock,
)
from city_sdk.utils.logging import bind

Brain = Callable[[Need, Market], Awaitable[None]]
KeyResolver = Callable[[str], Awaitable[PublicKey | None]]
CITY = "city:governance"
PROBER = "city:prober"


@dataclass
class OpenWork:
    msg_id: str
    fence: int
    need_id: str | None = None


def need_from(envelope: Mapping[str, Any]) -> Need:
    """Build the brain-facing Need. ``summary`` prefers ``body.summary``, then speech ``body.text``.

    The full structured body (including optional free speech) is on ``need.message`` (ADR-077).
    """

    body = envelope.get("body")
    if not isinstance(body, Mapping):
        return Need(message=message_from(envelope, {}))
    service = _text(body, "service", "service_type")
    district = body.get("district")
    emergency = body.get("is_emergency")
    return Need(
        service=service,
        district=district if type(district) is int else None,
        is_emergency=emergency is True,
        summary=_text(body, "summary", "text"),
        message=message_from(envelope, body),
    )


def message_from(envelope: Mapping[str, Any], body: Mapping[str, Any]) -> Message:
    def text(key: str) -> str | None:
        value = envelope.get(key)
        return value if isinstance(value, str) else None

    return Message(
        type=text("type") or "",
        sender=text("from") or "",
        msg_id=text("msg_id") or "",
        need_id=text("need_id"),
        conversation_id=text("conversation_id"),
        body=body,
    )


def _text(body: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


class Harness:
    def __init__(
        self,
        *,
        agent_id: str,
        private: PrivateKey,
        public: PublicKey,
        card: dict[str, Any],
        brain: Brain,
        clock: Clock | None = None,
        index: IndexPort | None = None,
        relay: HarnessRelay | None = None,
        ledger: HarnessLedger | None = None,
        attest: AttestPort | None = None,
        keys: dict[str, PublicKey] | None = None,
        defer: bool = False,
        log_per_second: int = 50,
        care: CarePort | None = None,
        resolve_key: KeyResolver | None = None,
        ack_delay: float | None = None,
        llm: LlmConnector | None = None,
    ) -> None:
        self.agent_id = agent_id
        self._private = private
        self._public = public
        self.public = public
        self.card = card
        self.brain = brain
        self.clock: Clock = clock if clock is not None else SystemClock()
        self.index = index if index is not None else MemoryIndex()
        self.relay = relay if relay is not None else MemoryRelay()
        self.ledger = ledger if ledger is not None else MemoryLedger()
        self.attest = attest if attest is not None else MemoryAttest()
        self.keys = {} if keys is None else keys
        self.care: CarePort = care if care is not None else MemoryCare(clock=self.clock)
        self.resolve_key = resolve_key
        self.llm = llm
        self.defer = defer
        # Seconds between a 202 and its final ack; None holds the ack until flush_acks().
        self.ack_delay = ack_delay
        self._pending_acks: dict[str, asyncio.Task[None] | None] = {}
        self._ack_tasks: set[asyncio.Task[None]] = set()
        # msg_id -> newest fence while the brain runs; the journal is written only after it.
        self._running: dict[str, int] = {}
        self._running_digest: dict[str, str] = {}
        self.journal = MemoryJournal()
        self.policy = PolicyStore()
        self.layers = LayerRegistry()
        self.memory: dict[str, str] = {}
        self.escalations: list[str] = []
        self.evidence: EvidencePort | None = None
        self.open: list[OpenWork] = []
        self.brain_calls = 0
        self.last_market: Market | None = None
        self.joined = False
        self.draining = False
        self.started = self.clock.now()
        self.log = HarnessLog(per_second=log_per_second, now=self.started)
        self._kid = agent_id + "#k1"

    async def start(self, market_id: str = "m-1") -> None:
        self._bind_log(market_id=market_id, category="lifecycle")
        self.log.info(
            "starting",
            now=self.clock.now(),
            agent_id=self.agent_id,
            market_id=market_id,
            category="lifecycle",
        )
        try:
            session = await self.relay.join(market_id)
        except CapError as exc:
            self.log.error(
                "join failed",
                now=self.clock.now(),
                agent_id=self.agent_id,
                market_id=market_id,
                code=exc.code,
                detail=exc.message,
                category="error",
                exc_info=True,
            )
            raise
        except Exception:
            self.log.error(
                "join failed",
                now=self.clock.now(),
                agent_id=self.agent_id,
                market_id=market_id,
                category="error",
                exc_info=True,
            )
            raise
        self.joined = True
        fields: dict[str, object] = {
            "agent_id": self.agent_id,
            "market_id": market_id,
            "category": "lifecycle",
        }
        for key in ("session_id", "lease_seconds", "policy_epoch"):
            value = session.get(key) if isinstance(session, dict) else None
            if value is not None:
                fields[key] = value
        self.log.info("joined", now=self.clock.now(), **fields)

    def _bind_log(self, **extra: object) -> None:
        team = self.card.get("team")
        version = self.card.get("version")
        bind(
            agent_id=self.agent_id,
            team_id=team if isinstance(team, str) else "",
            version=version if isinstance(version, str) else "",
            **extra,
        )

    async def shutdown(self) -> None:
        self.draining = True
        open_count = len(self.open)
        self.log.info(
            "draining",
            now=self.clock.now(),
            agent_id=self.agent_id,
            open=open_count,
            category="lifecycle",
        )
        # Acks still waiting are cancelled; acks already sending finish before the handback.
        for task in self._pending_acks.values():
            if task is not None:
                task.cancel()
        self._pending_acks.clear()
        await asyncio.gather(*self._ack_tasks, return_exceptions=True)
        for work in list(self.open):
            await self.relay.ack(msg_id=work.msg_id, fence=work.fence, status="retryable")
        self.open.clear()
        self.log.info("sigterm", now=self.clock.now(), agent_id=self.agent_id, category="lifecycle")

    def health(self, *, probe: str | None) -> dict[str, Any]:
        if self.draining:
            status = "draining"
        elif probe == "ready" and not self.joined:
            status = "degraded"
        else:
            status = "ok"
        uptime = int((self.clock.now() - self.started).total_seconds())
        models = self.card.get("dependencies")
        model_list: list[object] = []
        if isinstance(models, dict) and isinstance(models.get("models"), list):
            model_list = list(models["models"])
        return {
            "status": status,
            "agent_id": self.agent_id,
            "version": self.card["version"],
            "cap_versions": list(self.card["cap_versions"]),
            "profiles": list(self.card["profiles"]),
            "card_digest": card_digest(self.card),
            "policy": {"epoch": self.policy.epoch, "digest": self.policy.digest},
            "build": dict(self.card.get("build") or {}),
            "models": model_list,
            "uptime_s": max(uptime, 0),
        }

    async def invoke(self, payload: object) -> tuple[int, dict[str, Any]]:
        if self.draining:
            raise CapError("DRAINING", "shutting down")
        _ignored, envelope, msg_id, fence = _delivery(payload)
        fingerprint = digest(envelope)
        prior = self.journal.check(msg_id, fingerprint)
        if prior is not None and (prior.terminal or fence <= prior.fence):
            self.log.info(
                "duplicate",
                now=self.clock.now(),
                agent_id=self.agent_id,
                msg_id=msg_id,
                fence=fence,
                category="delivery",
            )
            return 409, dict(prior.body)
        if not isinstance(envelope.get("sig"), dict):
            raise CapError("SIGNATURE_INVALID", "sig is required", msg_id)
        # ADR-098: Relay already checked freshness at accept; do not 410 on queue lag.
        verify(
            _without_grant(envelope),
            now=self.clock.now(),
            key_for_kid=await self._keys_for(envelope),
            check_freshness=False,
        )
        if msg_id in self._running:
            if self._running_digest.get(msg_id) != fingerprint:
                raise CapError(
                    "IDEMPOTENCY_CONFLICT", "msg_id reused with different content", msg_id
                )
            return await self._hold_redelivery(msg_id, fence)
        if prior is not None:
            self.journal.move_fence(msg_id, fence)
            work = self._open_work(msg_id, fence, envelope.get("need_id"))
            await self.relay.renew(msg_id=msg_id, fence=fence)
            self._schedule_ack(work)
            self.log.info(
                "lease renewed",
                now=self.clock.now(),
                agent_id=self.agent_id,
                msg_id=msg_id,
                fence=fence,
                category="delivery",
            )
            held: dict[str, Any] = {"status": "received"}
            return 202, held
        traceparent = envelope.get("traceparent")
        trace = traceparent if isinstance(traceparent, str) else None
        need_id = envelope.get("need_id")
        msg_type = envelope.get("type")
        self.log.info(
            "delivery",
            now=self.clock.now(),
            agent_id=self.agent_id,
            msg_id=msg_id,
            need_id=need_id,
            fence=fence,
            type=msg_type,
            category="delivery",
        )
        self._bind_log(msg_id=msg_id, need_id=need_id, category="delivery")
        if msg_type == "cap.probe":
            body = {"status": "processed", "envelopes": [self._probe_result(envelope)]}
            self._remember(msg_id, fingerprint, 200, body, fence, terminal=True)
            self.log.info(
                "probe answered",
                now=self.clock.now(),
                agent_id=self.agent_id,
                msg_id=msg_id,
                category="delivery",
            )
            return 200, body
        need = need_from(envelope)
        market = self._market(trace, need.message)
        self.last_market = market
        self.brain_calls += 1
        started = time.perf_counter()
        self.log.info(
            "brain begin",
            now=self.clock.now(),
            agent_id=self.agent_id,
            msg_id=msg_id,
            need_id=need_id,
            service=need.service,
            district=need.district,
            category="delivery",
        )
        self._running[msg_id] = fence
        self._running_digest[msg_id] = fingerprint
        try:
            rejected = await self._think(need, market, msg_id)
        finally:
            latest = self._running.pop(msg_id, fence)
            self._running_digest.pop(msg_id, None)
        if rejected is not None:
            self._remember(msg_id, fingerprint, 200, rejected, latest, terminal=True)
            if latest != fence:
                # The relay dropped the first reply and holds the newer lease open.
                await self._settle_ack(msg_id, latest, "rejected")
            return 200, rejected
        fence = latest
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        self.log.info(
            "brain done",
            now=self.clock.now(),
            agent_id=self.agent_id,
            msg_id=msg_id,
            need_id=need_id,
            duration_ms=elapsed_ms,
            category="delivery",
        )
        if self.defer:
            # Journal before renew so a lease blip still yields 409 on replay
            # (invoke.idempotency). Return 202 even when renew fails.
            body = {"status": "received"}
            self._remember(msg_id, fingerprint, 202, body, fence, terminal=False)
            work = self._open_work(msg_id, fence, need_id)
            try:
                await self.relay.renew(msg_id=msg_id, fence=fence)
            except Exception:
                self.log.error(
                    "lease renew failed",
                    now=self.clock.now(),
                    agent_id=self.agent_id,
                    msg_id=msg_id,
                    fence=fence,
                    category="error",
                    exc_info=True,
                )
            self.log.info(
                "deferred",
                now=self.clock.now(),
                agent_id=self.agent_id,
                msg_id=msg_id,
                fence=fence,
                category="delivery",
            )
            self._schedule_ack(work)
            return 202, body
        await self.relay.ack(msg_id=msg_id, fence=fence, status="processed")
        body = {"status": "processed"}
        self._remember(msg_id, fingerprint, 200, body, fence, terminal=True)
        self.log.info(
            "ack sent",
            now=self.clock.now(),
            agent_id=self.agent_id,
            msg_id=msg_id,
            fence=fence,
            category="delivery",
        )
        return 200, body

    async def _think(self, need: Need, market: Market, msg_id: str) -> dict[str, Any] | None:
        """Run the brain; a rejection body when it declined or crashed, else None."""
        try:
            await self.brain(need, market)
        except OutOfScope:
            self.log.info(
                "out of scope",
                now=self.clock.now(),
                agent_id=self.agent_id,
                msg_id=msg_id,
                category="delivery",
            )
            return {
                "status": "rejected",
                "envelopes": [self._signed_ack(msg_id, "rejected", "OUT_OF_SCOPE")],
            }
        except Exception:
            self.log.error(
                "agent crashed",
                now=self.clock.now(),
                agent_id=self.agent_id,
                msg_id=msg_id,
                category="error",
                exc_info=True,
            )
            return {"status": "rejected", "envelopes": [self._signed_ack(msg_id, "rejected")]}
        return None

    async def _hold_redelivery(self, msg_id: str, fence: int) -> tuple[int, dict[str, Any]]:
        """The relay timed out on a brain still running: keep the lease, never run it twice."""
        if fence > self._running[msg_id]:
            self._running[msg_id] = fence
            try:
                await self.relay.renew(msg_id=msg_id, fence=fence)
            except Exception:
                self.log.error(
                    "lease renew failed",
                    now=self.clock.now(),
                    agent_id=self.agent_id,
                    msg_id=msg_id,
                    fence=fence,
                    category="error",
                    exc_info=True,
                )
        self.log.info(
            "held while running",
            now=self.clock.now(),
            agent_id=self.agent_id,
            msg_id=msg_id,
            fence=fence,
            category="delivery",
        )
        return 202, {"status": "received"}

    async def _settle_ack(self, msg_id: str, fence: int, status: str) -> None:
        try:
            await self.relay.ack(msg_id=msg_id, fence=fence, status=status)
        except Exception:
            self.log.error(
                "final ack failed",
                now=self.clock.now(),
                agent_id=self.agent_id,
                msg_id=msg_id,
                fence=fence,
                category="error",
                exc_info=True,
            )

    async def flush_acks(self) -> None:
        """Send every pending final ack now."""
        pending = list(self._pending_acks.items())
        self._pending_acks.clear()
        for msg_id, task in pending:
            if task is not None:
                task.cancel()
            work = next((item for item in self.open if item.msg_id == msg_id), None)
            if work is not None:
                await self._final_ack(work)

    def _open_work(self, msg_id: str, fence: int, need_id: object) -> OpenWork:
        for work in self.open:
            if work.msg_id == msg_id:
                work.fence = fence
                return work
        work = OpenWork(
            msg_id=msg_id, fence=fence, need_id=need_id if isinstance(need_id, str) else None
        )
        self.open.append(work)
        return work

    def _schedule_ack(self, work: OpenWork) -> None:
        if work.msg_id in self._pending_acks:
            return
        task: asyncio.Task[None] | None = None
        if self.ack_delay is not None:
            task = asyncio.get_running_loop().create_task(self._ack_after(work, self.ack_delay))
            self._ack_tasks.add(task)
            task.add_done_callback(self._ack_tasks.discard)
        self._pending_acks[work.msg_id] = task

    async def _ack_after(self, work: OpenWork, delay: float) -> None:
        await asyncio.sleep(delay)
        self._pending_acks.pop(work.msg_id, None)
        await self._final_ack(work)

    async def _final_ack(self, work: OpenWork) -> None:
        """The brain already ran; ``processed`` closes the delivery the 202 kept open."""
        fields: dict[str, object] = {
            "agent_id": self.agent_id,
            "msg_id": work.msg_id,
            "need_id": work.need_id,
            "fence": work.fence,
        }
        try:
            await self.relay.ack(msg_id=work.msg_id, fence=work.fence, status="processed")
        except Exception:
            self.log.error(
                "final ack failed",
                now=self.clock.now(),
                category="error",
                exc_info=True,
                **fields,
            )
            return
        if work in self.open:
            self.open.remove(work)
        self.log.info("final ack sent", now=self.clock.now(), category="delivery", **fields)

    async def alter(self, payload: object) -> tuple[int, dict[str, Any]]:
        if not isinstance(payload, dict):
            raise CapError("SCHEMA_INVALID", "body must be an object")
        verify(payload, now=self.clock.now(), key_for_kid=self._key_for_kid)
        if payload.get("from") != CITY or payload.get("type") != "city.order":
            raise CapError("SIGNATURE_INVALID", "order is not a city order")
        msg_raw = payload.get("msg_id")
        msg_id = msg_raw if isinstance(msg_raw, str) else None
        body = payload.get("body")
        if not isinstance(body, dict):
            raise CapError("SCHEMA_INVALID", "order body is required", msg_id)
        self.log.info(
            "order received",
            now=self.clock.now(),
            agent_id=self.agent_id,
            msg_id=msg_id,
            category="governance",
        )
        self.policy.apply(body, msg_id=msg_id)
        ack = self._order_ack(msg_id or "")
        self.log.info(
            "order applied",
            now=self.clock.now(),
            agent_id=self.agent_id,
            msg_id=msg_id,
            epoch=self.policy.epoch,
            category="governance",
        )
        return 200, ack

    def audit_entries(self, need_id: str | None) -> list[dict[str, Any]]:
        rows = self.log.lines
        if need_id is None:
            return [dict(row) for row in rows]
        return [dict(row) for row in rows if row.get("need_id") == need_id]

    def _market(self, traceparent: str | None, inbound: Message | None = None) -> Market:
        return Market(
            index=self.index,
            relay=self.relay,
            ledger=self.ledger,
            attest=self.attest,
            rank=self.layers.rank_plugin(),
            memory=self.memory,
            epoch=lambda: self.policy.epoch,
            traceparent=traceparent,
            escalations=self.escalations,
            evidence=self.evidence,
            agent_id=self.agent_id,
            care=self.care,
            messages=self.relay,
            inbound=inbound,
            llm=self.llm,
            notes=self.care if isinstance(self.care, NotesPort) else None,
        )

    def _remember(
        self,
        msg_id: str,
        fingerprint: str,
        status_code: int,
        body: dict[str, Any],
        fence: int,
        *,
        terminal: bool,
    ) -> None:
        self.journal.put(
            msg_id,
            JournalEntry(
                digest=fingerprint,
                status_code=status_code,
                body=dict(body),
                fence=fence,
                terminal=terminal,
            ),
        )

    def _probe_result(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        body = envelope.get("body")
        nonce = body.get("nonce") if isinstance(body, Mapping) else None
        if not isinstance(nonce, str):
            raise CapError("SCHEMA_INVALID", "nonce is required", _msg(envelope))
        unsigned = build(
            msg_id=str(uuid.uuid4()),
            message_type="cap.probe.result",
            sender=self.agent_id,
            recipient=PROBER,
            sent_at=self.clock.now(),
            body={"nonce": nonce},
            in_reply_to=_msg(envelope),
        )
        return sign(unsigned, self._private, kid=self._kid)

    def _signed_ack(self, in_reply_to: str, status: str, reason: str = "brain") -> dict[str, Any]:
        unsigned = build(
            msg_id=str(uuid.uuid4()),
            message_type="ack",
            sender=self.agent_id,
            recipient=CITY,
            sent_at=self.clock.now(),
            body={"status": status, "reason": reason},
            in_reply_to=in_reply_to,
        )
        return sign(unsigned, self._private, kid=self._kid)

    def _order_ack(self, in_reply_to: str) -> dict[str, Any]:
        unsigned = build(
            msg_id=str(uuid.uuid4()),
            message_type="city.order.ack",
            sender=self.agent_id,
            recipient=CITY,
            sent_at=self.clock.now(),
            body={"status": "applied", "applied_digest": self.policy.digest},
            in_reply_to=in_reply_to or None,
        )
        return sign(unsigned, self._private, kid=self._kid)

    async def _keys_for(self, envelope: Mapping[str, Any]) -> Callable[[str], PublicKey]:
        """Deploy-time keys first; an unknown kid is asked of the Index for this delivery only."""

        sig = envelope.get("sig")
        kid = sig.get("kid") if isinstance(sig, Mapping) else None
        sender = envelope.get("from")
        if (
            not isinstance(kid, str)
            or not isinstance(sender, str)
            or not kid.startswith(sender + "#")
            or kid in self.keys
            or self.resolve_key is None
        ):
            return self._key_for_kid
        try:
            found = await self.resolve_key(kid)
        except CapError:
            # UPSTREAM_TIMEOUT (504) must propagate so the Relay retries; other Cap
            # codes from Index stay as raised.
            raise
        except (KeyError, TypeError, ValueError, OSError):
            return self._key_for_kid
        if found is None:
            return self._key_for_kid

        def resolved(asked: str) -> PublicKey:
            return found if asked == kid else self._key_for_kid(asked)

        return resolved

    def _key_for_kid(self, kid: str) -> PublicKey:
        try:
            return self.keys[kid]
        except KeyError as exc:
            raise KeyError(kid) from exc


def _without_grant(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """The Relay checked the grant chain before accepting; the recipient has no grant to resolve
    (ADR-048). The JWS covers the envelope without ``sig``, so dropping ``sig.grant`` keeps it."""

    sig = envelope["sig"]
    if "grant" in sig and not isinstance(sig["grant"], str):
        raise CapError("SCHEMA_INVALID", "sig.grant must be a string", _msg(envelope))
    return {**envelope, "sig": {key: value for key, value in sig.items() if key != "grant"}}


def _msg(envelope: Mapping[str, Any]) -> str | None:
    msg_id = envelope.get("msg_id")
    return msg_id if isinstance(msg_id, str) else None


_INVOKE_FIELDS = frozenset({"delivery", "envelope"})
_DELIVERY_FIELDS = frozenset({"msg_id", "mailbox", "fence", "attempt", "lease_until"})
_ENVELOPE_FIELDS = frozenset(
    {
        "cap",
        "profile",
        "msg_id",
        "type",
        "from",
        "to",
        "conversation_id",
        "need_id",
        "in_reply_to",
        "fence",
        "sent_at",
        "expires_at",
        "traceparent",
        "body",
        "sig",
    }
)


def _unknown(keys: object, allowed: frozenset[str]) -> bool:
    if not isinstance(keys, dict):
        return False
    for key in keys:
        if key in allowed or key.startswith("x-"):
            continue
        return True
    return False


def _delivery(payload: object) -> tuple[dict[str, Any], dict[str, Any], str, int]:
    if not isinstance(payload, dict):
        raise CapError("SCHEMA_INVALID", "body must be an object")
    if _unknown(payload, _INVOKE_FIELDS):
        raise CapError("SCHEMA_INVALID", "unknown field")
    delivery = payload.get("delivery")
    envelope = payload.get("envelope")
    if isinstance(delivery, dict) and _unknown(delivery, _DELIVERY_FIELDS):
        raise CapError("SCHEMA_INVALID", "unknown field")
    if isinstance(envelope, dict) and _unknown(envelope, _ENVELOPE_FIELDS):
        raise CapError("SCHEMA_INVALID", "unknown field")
    if not isinstance(delivery, dict) or not isinstance(envelope, dict):
        raise CapError("SCHEMA_INVALID", "delivery and envelope are required")
    msg_id = envelope.get("msg_id")
    if not isinstance(msg_id, str) or delivery.get("msg_id") != msg_id:
        raise CapError("SCHEMA_INVALID", "msg_id mismatch")
    fence = delivery.get("fence")
    if type(fence) is not int or fence < 1:
        raise CapError("SCHEMA_INVALID", "fence is required", msg_id)
    return delivery, envelope, msg_id, fence
