"""Minimal OpenAI-compatible client for the City LLM Gateway.

``chat_completion`` is the blocking call for scripts on a laptop. ``AsyncGateway`` is what the
harness uses behind ``market.llm``. Neither retries: a refusal surfaces once as a CAP error.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

import httpx
from cap.core.errors import HTTP_STATUS, CapError

ALIAS = "city-default"
PATH = "/llm/v1/chat/completions"
TIMEOUT_S = 60.0


def chat_completion(
    *,
    gateway_url: str,
    virtual_key: str,
    messages: list[dict[str, str]],
    model: str = ALIAS,
) -> str:
    """POST ``/llm/v1/chat/completions`` and return assistant content."""
    if not virtual_key.strip():
        raise CapError("KEY_UNKNOWN", "virtual key")
    base = gateway_url.rstrip("/")
    body = json.dumps({"model": model, "messages": messages}).encode("utf-8")
    request = urllib.request.Request(
        f"{base}{PATH}",
        data=body,
        headers={
            "Authorization": f"Bearer {virtual_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            payload: Any = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise _cap_from(exc.code, exc.read().decode("utf-8", errors="replace")) from None
    except urllib.error.URLError:
        raise CapError("UPSTREAM_TIMEOUT", "llm gateway") from None
    return str(answer(payload)["content"])


class AsyncGateway:
    """``GatewayPort`` over HTTP: one POST per call, the key only in the header."""

    def __init__(self, gateway_url: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._url = gateway_url.rstrip("/") + PATH
        self._client = client if client is not None else httpx.AsyncClient(timeout=TIMEOUT_S)

    async def agent_completion(
        self, virtual_key: str, body: Mapping[str, object]
    ) -> Mapping[str, object]:
        try:
            response = await self._client.post(
                self._url,
                json=dict(body),
                headers={"Authorization": f"Bearer {virtual_key}"},
            )
        except httpx.HTTPError:
            raise CapError("UPSTREAM_TIMEOUT", "llm gateway") from None
        if response.status_code >= 400:
            raise _cap_from(response.status_code, response.text)
        try:
            payload: Any = response.json()
        except ValueError:
            raise CapError("SCHEMA_INVALID", "llm response") from None
        return answer(payload)


def answer(payload: Any) -> dict[str, object]:
    """The Gateway's ``{model, content, *_tokens}``, or the same read from an OpenAI shape."""
    if not isinstance(payload, dict):
        raise CapError("SCHEMA_INVALID", "llm response")
    content = payload.get("content")
    prompt = payload.get("prompt_tokens")
    completion = payload.get("completion_tokens")
    if not isinstance(content, str):
        content = ""
        choices = payload.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                content = message["content"]
        usage = payload.get("usage")
        if isinstance(usage, dict):
            prompt = usage.get("prompt_tokens")
            completion = usage.get("completion_tokens")
    model = payload.get("model")
    return {
        "model": model if isinstance(model, str) else ALIAS,
        "content": content,
        "prompt_tokens": prompt if type(prompt) is int else 0,
        "completion_tokens": completion if type(completion) is int else 0,
    }


def _cap_from(status: int, raw: str) -> CapError:
    code: str | None = None
    message = "llm gateway"
    try:
        parsed: Any = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        maybe_code = parsed.get("code")
        maybe_message = parsed.get("message")
        if isinstance(maybe_code, str):
            code = maybe_code
        if isinstance(maybe_message, str) and maybe_message:
            message = maybe_message
    if code is not None and code in HTTP_STATUS:
        return CapError(code, message)
    if status == 401:
        return CapError("KEY_UNKNOWN", message)
    if status == 429:
        return CapError("RATE_LIMITED", message)
    return CapError("UPSTREAM_TIMEOUT", message)
