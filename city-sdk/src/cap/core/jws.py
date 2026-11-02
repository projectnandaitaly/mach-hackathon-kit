"""Detached compact JWS (``alg=EdDSA``) over caller-supplied payload bytes."""

from __future__ import annotations

import base64
import json
from typing import cast

from cap.core.errors import CapError
from cap.core.jcs import canonicalize
from cap.core.keys import PrivateKey, PublicKey


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + pad)
    except ValueError as exc:
        raise CapError("SIGNATURE_INVALID", "JWS segment is not base64url") from exc


def _header(kid: str | None) -> dict[str, str]:
    header = {"alg": "EdDSA"}
    if kid is not None:
        header["kid"] = kid
    return header


def sign_detached(private: PrivateKey, payload: bytes, *, kid: str | None = None) -> str:
    """Return ``base64url(header)..base64url(signature)`` (empty payload segment)."""
    protected = _b64url(canonicalize(_header(kid)).encode("utf-8"))
    signing_input = protected.encode("ascii") + b"." + _b64url(payload).encode("ascii")
    signature = private.sign(signing_input)
    return protected + ".." + _b64url(signature)


def verify_detached(
    public: PublicKey,
    payload: bytes,
    token: str,
    *,
    kid: str | None = None,
) -> None:
    parts = token.split(".")
    if len(parts) != 3 or parts[1] != "":
        raise CapError("SIGNATURE_INVALID", "JWS is not detached compact")
    protected_b64, _, signature_b64 = parts
    try:
        header_raw = _b64url_decode(protected_b64)
        header = cast(dict[str, object], json.loads(header_raw.decode("utf-8")))
    except (CapError, UnicodeError, json.JSONDecodeError) as exc:
        raise CapError("SIGNATURE_INVALID", "JWS header is not JSON") from exc
    if header.get("alg") != "EdDSA":
        raise CapError("SIGNATURE_INVALID", "JWS alg must be EdDSA")
    if kid is not None and header.get("kid") not in (None, kid):
        raise CapError("KEY_UNKNOWN", "JWS kid does not match")
    signing_input = protected_b64.encode("ascii") + b"." + _b64url(payload).encode("ascii")
    public.verify(signing_input, _b64url_decode(signature_b64))
