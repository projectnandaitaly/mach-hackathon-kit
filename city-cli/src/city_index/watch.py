"""SSE fan-out for Index watchers."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator
from typing import Any


class WatchHub:
    def __init__(self, *, queue_size: int = 100, replay_size: int = 500) -> None:
        self._queue_size = queue_size
        self._replay: deque[dict[str, Any]] = deque(maxlen=replay_size)
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []

    def subscribe(self, *, last_event_id: str | None = None) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        replay = list(self._replay)
        if last_event_id is not None:
            positions = [
                index
                for index, event in enumerate(replay)
                if event.get("record_id") == last_event_id
            ]
            replay = replay[positions[-1] + 1 :] if positions else []
        else:
            replay = []
        for event in replay[-self._queue_size :]:
            queue.put_nowait(event)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    async def publish(self, event: dict[str, Any]) -> None:
        self._replay.append(dict(event))
        for queue in list(self._subscribers):
            if queue.full():
                self.unsubscribe(queue)
                continue
            queue.put_nowait(dict(event))

    async def stream(self, queue: asyncio.Queue[dict[str, Any]]) -> AsyncIterator[str]:
        try:
            while True:
                event = await queue.get()
                payload = json.dumps(event, separators=(",", ":"))
                event_id = event.get("record_id")
                prefix = f"id: {event_id}\n" if isinstance(event_id, str) else ""
                yield f"{prefix}event: {event.get('type', 'message')}\ndata: {payload}\n\n"
        finally:
            self.unsubscribe(queue)
