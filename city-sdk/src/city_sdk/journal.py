"""Append-only idempotency journal. Same bytes, same outcome."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cap.core.errors import CapError


@dataclass
class JournalEntry:
    digest: str
    status_code: int
    body: dict[str, Any]
    fence: int
    terminal: bool


class MemoryJournal:
    def __init__(self) -> None:
        self._rows: dict[str, JournalEntry] = {}

    def get(self, msg_id: str) -> JournalEntry | None:
        return self._rows.get(msg_id)

    def put(self, msg_id: str, entry: JournalEntry) -> None:
        prior = self._rows.get(msg_id)
        if prior is not None:
            if prior.digest != entry.digest:
                raise CapError(
                    "IDEMPOTENCY_CONFLICT",
                    "msg_id reused with different content",
                    msg_id,
                )
            return
        self._rows[msg_id] = entry

    def move_fence(self, msg_id: str, fence: int) -> None:
        prior = self._rows[msg_id]
        self._rows[msg_id] = JournalEntry(
            digest=prior.digest,
            status_code=prior.status_code,
            body=dict(prior.body),
            fence=fence,
            terminal=prior.terminal,
        )

    def check(self, msg_id: str, digest: str) -> JournalEntry | None:
        prior = self._rows.get(msg_id)
        if prior is None:
            return None
        if prior.digest != digest:
            raise CapError(
                "IDEMPOTENCY_CONFLICT",
                "msg_id reused with different content",
                msg_id,
            )
        return prior
