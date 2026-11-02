"""Build, sign, and verify a CAP envelope."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta

from cap.core.errors import CapError
from cap.core.grant import SignedGrant
from cap.core.grant import verify as verify_grant
from cap.core.jcs import canonicalize
from cap.core.jws import sign_detached, verify_detached
from cap.core.keys import PrivateKey, PublicKey
from cap.core.timeutil import format_time, parse_time

_SKEW = timedelta(seconds=120)
_DEFAULT_TTL = timedelta(seconds=60)


def build(
    *,
    msg_id: str,
    message_type: str,
    sender: str,
    recipient: str,
    sent_at: datetime,
    body: Mapping[str, object],
    profile: str | None = None,
    expires_at: datetime | None = None,
    conversation_id: str | None = None,
    need_id: str | None = None,
    in_reply_to: str | None = None,
    traceparent: str | None = None,
) -> dict[str, object]:
    envelope: dict[str, object] = {
        "cap": "1.0",
        "msg_id": msg_id,
        "type": message_type,
        "from": sender,
        "to": recipient,
        "sent_at": format_time(sent_at),
        "body": dict(body),
    }
    if profile is not None:
        envelope["profile"] = profile
    if expires_at is not None:
        envelope["expires_at"] = format_time(expires_at)
    if conversation_id is not None:
        envelope["conversation_id"] = conversation_id
    if need_id is not None:
        envelope["need_id"] = need_id
    if in_reply_to is not None:
        envelope["in_reply_to"] = in_reply_to
    if traceparent is not None:
        envelope["traceparent"] = traceparent
    return envelope


def sign(
    envelope: Mapping[str, object],
    private: PrivateKey,
    *,
    kid: str,
    grant_id: str | None = None,
) -> dict[str, object]:
    unsigned = {key: value for key, value in envelope.items() if key != "sig"}
    sender = unsigned.get("from")
    if not isinstance(sender, str) or not kid.startswith(sender + "#"):
        raise CapError("KEY_UNKNOWN", "kid is not under from")
    sig: dict[str, str] = {"alg": "EdDSA", "kid": kid}
    if grant_id is not None:
        sig["grant"] = grant_id
    sig["jws"] = sign_detached(private, canonicalize(unsigned).encode("utf-8"), kid=kid)
    signed = dict(unsigned)
    signed["sig"] = sig
    return signed


def verify(
    envelope: Mapping[str, object],
    *,
    now: datetime,
    key_for_kid: Callable[[str], PublicKey],
    grant_for_id: Callable[[str], SignedGrant] | None = None,
    controller_public: PublicKey | Sequence[PublicKey] | None = None,
    required_permission: str | None = None,
    target_visible: Callable[[str], bool] | None = None,
    record_verifier: Callable[[Mapping[str, object]], None] | None = None,
) -> None:
    """Verify signature, ``from``/``kid``, grant chain, expiry, and clock skew.

    ``target_visible`` defaults to true. The Relay supplies the Index check.
    """
    if now.tzinfo is None:
        raise CapError("SCHEMA_INVALID", "now must be timezone-aware")
    msg_id = envelope.get("msg_id")
    msg = msg_id if isinstance(msg_id, str) else None
    unsigned = {key: value for key, value in envelope.items() if key != "sig"}
    for field in ("cap", "msg_id", "type", "from", "to", "sent_at", "body"):
        if field not in unsigned:
            raise CapError("SCHEMA_INVALID", f"missing {field}", msg)
    if unsigned.get("cap") != "1.0":
        raise CapError("SCHEMA_INVALID", "cap must be 1.0", msg)
    sender = unsigned["from"]
    recipient = unsigned["to"]
    if not isinstance(sender, str) or not isinstance(recipient, str):
        raise CapError("SCHEMA_INVALID", "from and to must be strings", msg)
    sig = envelope.get("sig")
    if not isinstance(sig, Mapping):
        raise CapError("SCHEMA_INVALID", "sig is required", msg)
    if sig.get("alg") != "EdDSA":
        raise CapError("SIGNATURE_INVALID", "sig.alg must be EdDSA", msg)
    kid = sig.get("kid")
    token = sig.get("jws")
    if not isinstance(kid, str) or not isinstance(token, str):
        raise CapError("SCHEMA_INVALID", "sig.kid and sig.jws are required", msg)
    if not kid.startswith(sender + "#"):
        raise CapError("KEY_UNKNOWN", "kid is not under from", msg)
    try:
        public = key_for_kid(kid)
    except KeyError as exc:
        raise CapError("KEY_UNKNOWN", kid, msg) from exc
    verify_detached(public, canonicalize(unsigned).encode("utf-8"), token, kid=kid)
    grant_id = sig.get("grant")
    if grant_id is not None:
        if not isinstance(grant_id, str):
            raise CapError("SCHEMA_INVALID", "sig.grant must be a string", msg)
        if grant_for_id is None or controller_public is None:
            raise CapError("SIGNATURE_INVALID", "grant resolver is required", msg)
        try:
            signed_grant = grant_for_id(grant_id)
        except KeyError as exc:
            raise CapError("SIGNATURE_INVALID", "unknown grant", msg) from exc
        if signed_grant.grant.get("grant_id") != grant_id:
            raise CapError("SIGNATURE_INVALID", "grant_id mismatch", msg)
        if signed_grant.grant.get("agent_id") != sender:
            raise CapError("SIGNATURE_INVALID", "grant agent_id mismatch", msg)
        if signed_grant.grant.get("agent_pubkey") != public.to_jwk():
            raise CapError("KEY_UNKNOWN", "grant key does not match kid", msg)
        verify_grant(
            signed_grant,
            controller_public,
            now=now,
            required_permission=required_permission,
            record_verifier=record_verifier,
        )
    sent_at = parse_time(unsigned["sent_at"])
    if abs(now - sent_at) > _SKEW:
        raise CapError("EXPIRED", "sent_at outside ±120s", msg)
    expires_raw = unsigned.get("expires_at")
    expires_at = parse_time(expires_raw) if expires_raw is not None else sent_at + _DEFAULT_TTL
    if now > expires_at:
        raise CapError("EXPIRED", "expires_at passed", msg)
    visible = True if target_visible is None else target_visible(recipient)
    if not visible:
        raise CapError("TARGET_NOT_VISIBLE", recipient, msg)
