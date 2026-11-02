"""RFC 8785 canonical JSON and SHA-256 digests."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import cast

import rfc8785

Json = dict[str, "Json"] | list["Json"] | str | int | float | bool | None


def canonicalize(value: object) -> str:
    """Return the RFC 8785 canonical JSON text for ``value``."""
    raw = rfc8785.dumps(value)  # type: ignore[arg-type]
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    return str(raw)


def digest(value: object) -> str:
    """SHA-256 over the UTF-8 canonical form, prefixed ``sha256:``."""
    raw = canonicalize(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def card_digest(card: Mapping[str, object]) -> str:
    """SHA-256 over JCS of the card, excluding ``signatures.city`` (ADR-013)."""
    data = dict(card)
    signatures = data.get("signatures")
    if isinstance(signatures, Mapping):
        kept = {key: value for key, value in signatures.items() if key != "city"}
        if kept:
            data["signatures"] = kept
        else:
            del data["signatures"]
    return digest(cast(Json, data))
