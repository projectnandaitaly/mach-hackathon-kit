"""Deployment Grant issuance and verification (``SignedGrant { grant, jws }``)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from cap.core.errors import CapError
from cap.core.jcs import canonicalize
from cap.core.jws import sign_detached, verify_detached
from cap.core.keys import PrivateKey, PublicKey
from cap.core.timeutil import parse_time

# One teammate's key, or every key the team has registered.
ControllerKeys = PublicKey | Sequence[PublicKey]

_REQUIRED = (
    "grant_id",
    "agent_id",
    "version",
    "agent_pubkey",
    "permissions",
    "not_before",
    "not_after",
)


@dataclass(frozen=True)
class SignedGrant:
    grant: dict[str, object]
    jws: str


def issue(
    controller: PrivateKey,
    grant: Mapping[str, object],
    *,
    kid: str | None = None,
) -> SignedGrant:
    """Controller signs the JCS of the grant object. The signature sits beside it."""
    payload = {key: grant[key] for key in grant}
    _require_fields(payload)
    token = sign_detached(controller, canonicalize(payload).encode("utf-8"), kid=kid)
    return SignedGrant(grant=payload, jws=token)


def signing_key(signed: SignedGrant, controller: ControllerKeys) -> PublicKey:
    """The team key that signed ``signed``. Any registered teammate key qualifies."""

    _require_fields(signed.grant)
    keys = (controller,) if isinstance(controller, PublicKey) else tuple(controller)
    payload = canonicalize(signed.grant).encode("utf-8")
    last: CapError | None = None
    for key in keys:
        try:
            verify_detached(key, payload, signed.jws)
        except CapError as exc:
            last = exc
            continue
        return key
    if last is not None:
        raise last
    raise CapError("KEY_UNKNOWN", "no controller for grant")


def verify(
    signed: SignedGrant,
    controller: ControllerKeys,
    *,
    now: datetime,
    required_permission: str | None = None,
    record_verifier: Callable[[Mapping[str, object]], None] | None = None,
) -> None:
    """Verify the controller signature, the window, and an optional permission.

    ``controller`` may be every key registered for the team. The grant matches
    when any one of them signed it.

    ``record_verifier`` is the city-root check for a Team Registration Record.
    Issuing that countersignature stays with Deploy / KMS.
    """
    if now.tzinfo is None:
        raise CapError("SCHEMA_INVALID", "now must be timezone-aware")
    signing_key(signed, controller)
    if record_verifier is not None:
        record_verifier(signed.grant)
    not_before = parse_time(signed.grant["not_before"])
    not_after = parse_time(signed.grant["not_after"])
    if now < not_before or now > not_after:
        raise CapError("GRANT_EXPIRED", "grant is outside not_before/not_after")
    if required_permission is not None:
        permissions = signed.grant["permissions"]
        if not isinstance(permissions, list) or required_permission not in permissions:
            raise CapError("GRANT_PERMISSION_DENIED", required_permission)


def _require_fields(grant: Mapping[str, object]) -> None:
    missing = [key for key in _REQUIRED if key not in grant]
    if missing:
        raise CapError("SCHEMA_INVALID", "grant missing " + ",".join(missing))
    pubkey = grant["agent_pubkey"]
    if not isinstance(pubkey, Mapping):
        raise CapError("SCHEMA_INVALID", "agent_pubkey must be an object")
    permissions = grant["permissions"]
    if not isinstance(permissions, list):
        raise CapError("SCHEMA_INVALID", "permissions must be an array")
