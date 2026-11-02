"""Checkpoint sink for deploy state. The memory service leaves it unset."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol


class StateSink(Protocol):
    def checkpoint(self, snapshot: Mapping[str, object]) -> None: ...
