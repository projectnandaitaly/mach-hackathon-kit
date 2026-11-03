"""Published ``score-evaluator@2`` reputations for query results (ADR-085).

The Index reads numbers and shows them. It never sorts, filters, or weights by them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Protocol

log = logging.getLogger("city.index.reputation")

Scores = dict[str, tuple[int | float, int]]
REFRESH = timedelta(seconds=60)


class ReputationPort(Protocol):
    def scores(self, now: datetime) -> Scores: ...


class NoReputation:
    def scores(self, now: datetime) -> Scores:
        del now
        return {}


class PublishedReputation:
    """``read`` returns the published report or None. A failure keeps the last numbers."""

    def __init__(
        self,
        read: Callable[[], Mapping[str, object] | None],
        *,
        refresh: timedelta = REFRESH,
    ) -> None:
        self._read = read
        self._refresh = refresh
        self._cached: Scores = {}
        self._at: datetime | None = None

    def scores(self, now: datetime) -> Scores:
        if self._at is not None and now - self._at < self._refresh:
            return self._cached
        try:
            report = self._read()
        except Exception as exc:
            log.warning("reputation report unavailable", extra={"error": type(exc).__name__})
            self._at = now
            return self._cached
        self._cached = parse_report(report)
        self._at = now
        return self._cached


def parse_report(report: Mapping[str, object] | None) -> Scores:
    found: Scores = {}
    rows = report.get("reputations") if report is not None else None
    if not isinstance(rows, list):
        return found
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("agent_id"), str):
            continue
        value = row.get("reputation", 0)
        count = row.get("rating_count", 0)
        numeric = isinstance(value, int | float) and not isinstance(value, bool)
        reputation: int | float = value if numeric and isinstance(value, int | float) else 0
        ratings = count if isinstance(count, int) and not isinstance(count, bool) else 0
        ratings = max(ratings, 0)
        found[str(row["agent_id"])] = (reputation, ratings)
    return found
