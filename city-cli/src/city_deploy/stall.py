"""Log where the event loop is stuck when it stops answering.

A task on the loop stamps a heartbeat. A daemon thread reads it, and once the loop
has been silent past ``threshold`` it logs the loop thread's Python stack. A long
stall is logged again every ``threshold`` seconds, so the frames show what held it.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback
from collections.abc import Callable

_LOG = logging.getLogger("city.deploy.stall")


class StallWatch:
    def __init__(
        self,
        *,
        threshold: float = 1.0,
        beat: float = 0.2,
        report: Callable[[float, str], None] | None = None,
    ) -> None:
        self.threshold = threshold
        self.beat = beat
        self._report = report or _log
        self._last = time.monotonic()
        self._loop_thread: int | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        """Call on the event loop."""

        self._loop_thread = threading.get_ident()
        self._last = time.monotonic()
        self._task = asyncio.get_running_loop().create_task(self._heartbeat())
        threading.Thread(target=self._watch, name="stall-watch", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()

    async def _heartbeat(self) -> None:
        while not self._stop.is_set():
            self._last = time.monotonic()
            await asyncio.sleep(self.beat)

    def _watch(self) -> None:
        reported = 0.0
        while not self._stop.wait(self.beat):
            lag = time.monotonic() - self._last
            if lag < self.threshold:
                reported = 0.0
                continue
            if lag - reported < self.threshold:
                continue
            reported = lag
            frame = sys._current_frames().get(self._loop_thread or 0)
            stack = "".join(traceback.format_stack(frame, limit=15)) if frame else ""
            self._report(lag, stack)


def _log(lag: float, stack: str) -> None:
    _LOG.warning("event loop stalled", extra={"seconds": round(lag, 2), "detail": stack})
