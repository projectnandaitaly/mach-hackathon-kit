"""Markdown report and a signed evidence bundle. The signing key is ephemeral."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from cap.core.envelope import build, sign, verify
from cap.core.keys import PublicKey
from city.testkit import ephemeral

from city_conform.runner import Report


def render(report: Report) -> str:
    lines = [
        "# Conformance",
        "",
        "| Stage | Name | Outcome | Detail |",
        "| --- | --- | --- | --- |",
    ]
    for row in report.rows:
        detail = row.detail.replace("|", "/")
        lines.append(f"| {row.number} | {row.name} | {row.outcome.value} | {detail} |")
    lines.append("")
    lines.append(f"Profile: {'PASS' if report.passed else 'FAIL'}")
    return "\n".join(lines) + "\n"


def sign_report(report: Report, *, now: datetime) -> tuple[dict[str, Any], PublicKey]:
    keys = ephemeral()
    unsigned = build(
        msg_id=str(uuid.uuid4()),
        message_type="conformance.result",
        sender="city:conformance",
        recipient="city:governance",
        sent_at=now,
        body={"profile": "full", "stages": [row.as_dict() for row in report.rows]},
    )
    envelope = sign(unsigned, keys.private, kid="city:conformance#k1")
    return envelope, keys.public


def verify_report(envelope: dict[str, Any], public: PublicKey, *, now: datetime) -> dict[str, Any]:
    verify(envelope, now=now, key_for_kid=lambda _kid: public)
    body = envelope.get("body")
    if not isinstance(body, dict):
        raise ValueError("bundle body")
    return body
