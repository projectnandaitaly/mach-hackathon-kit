"""Injectable clock. Tests do not sleep, and production reads real UTC."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    def __init__(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("clock must be timezone-aware")
        self.moment = moment

    def now(self) -> datetime:
        return self.moment

    def advance(self, *, minutes: int = 0, seconds: int = 0) -> None:
        self.moment = self.moment + timedelta(minutes=minutes, seconds=seconds)
