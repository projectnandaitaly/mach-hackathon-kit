"""Generate an Ed25519 key pair in memory. The private key is not serialized."""

from __future__ import annotations

from cap.core.keys import KeyPair, generate


def ephemeral() -> KeyPair:
    return generate()
