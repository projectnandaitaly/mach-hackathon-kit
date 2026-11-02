"""Ed25519 keys and public JWK (OKP / Ed25519).

``repr`` and ``to_jwk`` omit the private scalar. ``to_pem`` is only for the
secret store. Callers must not log that PEM or write it into the repo.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from cap.core.errors import CapError


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + pad)
    except ValueError as exc:
        raise CapError("SCHEMA_INVALID", "JWK x is not base64url") from exc


@dataclass(frozen=True)
class PublicKey:
    _key: Ed25519PublicKey

    def to_jwk(self) -> dict[str, str]:
        raw = self._key.public_bytes(encoding=Encoding.Raw, format=PublicFormat.Raw)
        return {"kty": "OKP", "crv": "Ed25519", "x": _b64url(raw)}

    @classmethod
    def from_jwk(cls, jwk: Mapping[str, object]) -> PublicKey:
        if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
            raise CapError("SCHEMA_INVALID", "JWK must be OKP Ed25519")
        if "d" in jwk:
            raise CapError("SCHEMA_INVALID", "public JWK must not carry a private field")
        x = jwk.get("x")
        if not isinstance(x, str):
            raise CapError("SCHEMA_INVALID", "JWK x is required")
        raw = _b64url_decode(x)
        if len(raw) != 32:
            raise CapError("SCHEMA_INVALID", "Ed25519 public key must be 32 bytes")
        return cls(Ed25519PublicKey.from_public_bytes(raw))

    def verify(self, data: bytes, signature: bytes) -> None:
        try:
            self._key.verify(signature, data)
        except InvalidSignature as exc:
            raise CapError("SIGNATURE_INVALID", "signature mismatch") from exc


@dataclass(frozen=True)
class PrivateKey:
    _key: Ed25519PrivateKey

    def __repr__(self) -> str:
        return "PrivateKey()"

    def public_key(self) -> PublicKey:
        return PublicKey(self._key.public_key())

    def sign(self, data: bytes) -> bytes:
        return self._key.sign(data)

    def to_pem(self) -> str:
        raw = self._key.private_bytes(
            encoding=Encoding.PEM,
            format=PrivateFormat.PKCS8,
            encryption_algorithm=NoEncryption(),
        )
        return raw.decode("ascii")


@dataclass(frozen=True)
class KeyPair:
    private: PrivateKey
    public: PublicKey


def generate() -> KeyPair:
    private = Ed25519PrivateKey.generate()
    wrapped = PrivateKey(private)
    return KeyPair(private=wrapped, public=wrapped.public_key())
