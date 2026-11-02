"""Query cache with TTL and invalidation (Memorystore-shaped)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol


class QueryCachePort(Protocol):
    def get(self, key: str, *, now: datetime) -> list[dict[str, Any]] | None: ...

    def put(self, key: str, value: list[dict[str, Any]], *, now: datetime) -> None: ...

    def invalidate(self) -> None: ...


@dataclass
class _Entry:
    value: list[dict[str, Any]]
    expires_at: datetime


class QueryCache:
    def __init__(self, *, ttl_seconds: float = 5.0) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._entries: dict[str, _Entry] = {}

    def get(self, key: str, *, now: datetime) -> list[dict[str, Any]] | None:
        entry = self._entries.get(key)
        if entry is None or now >= entry.expires_at:
            self._entries.pop(key, None)
            return None
        return [dict(item) for item in entry.value]

    def put(self, key: str, value: list[dict[str, Any]], *, now: datetime) -> None:
        self._entries[key] = _Entry(
            value=[dict(item) for item in value],
            expires_at=now + self._ttl,
        )

    def invalidate(self) -> None:
        self._entries.clear()
