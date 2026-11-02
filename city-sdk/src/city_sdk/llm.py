"""Harness llm: connector. The brain never sees a key, a URL, or the alias table."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Protocol

from cap.core.errors import CapError

DIRECT = "gemini-direct"
GATEWAY = "city-gateway"
ALIAS = "city-default"


class DirectBackend(Protocol):
    def complete(
        self,
        *,
        model: str,
        messages: list[object],
        system: str | None,
    ) -> Mapping[str, object]: ...


class GatewayPort(Protocol):
    async def agent_completion(
        self, virtual_key: str, body: Mapping[str, object]
    ) -> Mapping[str, object]: ...


class LlmConnector:
    def __init__(
        self,
        *,
        provider: str,
        handle: str | None = None,
        direct: DirectBackend | None = None,
        gateway: GatewayPort | None = None,
        virtual_key: str | None = None,
    ) -> None:
        self._provider = provider
        self._handle = handle
        self._direct = direct
        self._gateway = gateway
        self._virtual_key = virtual_key

    async def complete(
        self, messages: list[object], *, need_id: str | None = None
    ) -> dict[str, object]:
        if self._provider == DIRECT:
            if not self._handle or self._direct is None:
                raise CapError("KEY_UNKNOWN", "gemini handle is missing")
            raw = await asyncio.to_thread(
                self._direct.complete, model=ALIAS, messages=messages, system=None
            )
            return _public(raw)
        if self._provider == GATEWAY:
            if self._gateway is None or not self._virtual_key:
                raise CapError("KEY_UNKNOWN", "virtual key is missing")
            body: dict[str, object] = {"model": ALIAS, "messages": messages}
            if need_id:
                body["need_id"] = need_id
            raw = await self._gateway.agent_completion(self._virtual_key, body)
            return _public(raw)
        raise CapError("SCHEMA_INVALID", "provider")


def _public(raw: Mapping[str, object]) -> dict[str, object]:
    content = raw.get("content")
    model = raw.get("model")
    prompt = raw.get("prompt_tokens")
    completion = raw.get("completion_tokens")
    return {
        "model": model if isinstance(model, str) else ALIAS,
        "content": content if isinstance(content, str) else "",
        "prompt_tokens": prompt if type(prompt) is int else 0,
        "completion_tokens": completion if type(completion) is int else 0,
    }
