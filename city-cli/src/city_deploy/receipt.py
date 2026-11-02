"""Compliance Receipt. The signing key is ephemeral and is not stored."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from cap.core.envelope import build, sign
from cap.core.keys import PublicKey
from city.testkit import ephemeral


def sign_receipt(
    *,
    agent_id: str,
    image_digest: str,
    now: datetime,
) -> tuple[dict[str, Any], PublicKey]:
    keys = ephemeral()
    unsigned = build(
        msg_id=str(uuid4()),
        message_type="compliance.receipt",
        sender="city:deploy",
        recipient="city:governance",
        sent_at=now,
        body={
            "agent_id": agent_id,
            "image_digest": image_digest,
            "base_image": "city-sdk",
        },
    )
    envelope = sign(unsigned, keys.private, kid="city:deploy#k1")
    return envelope, keys.public
