"""Ports for clock and event publishing."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """UTC wall clock for production services."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class EventPublisher(Protocol):
    async def publish(self, record: Mapping[str, object]) -> None: ...
