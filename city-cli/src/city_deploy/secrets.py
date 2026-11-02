"""Memory secret port. Values are not returned by status or logs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from secrets import token_hex
from typing import Protocol

from cap.core.errors import CapError
from cap.core.keys import PrivateKey, PublicKey

OVERLAP = timedelta(minutes=10)


class SecretPort(Protocol):
    def hold_agent_key(self, agent_id: str, private: PrivateKey) -> None: ...

    def agent_key(self, agent_id: str) -> PrivateKey:
        """In-process signer. The scalar is not serialized."""
        ...

    def pod_material(self, agent_id: str) -> Mapping[str, str] | None:
        """Signing PEM and signed grant for a pod mount. Do not log the result."""
        ...

    def set_secret(self, agent_id: str, name: str, value: str) -> None: ...

    def issue_llm(self, agent_id: str) -> None: ...

    def rotate_llm(self, agent_id: str) -> None: ...

    def revoke_llm(self, agent_id: str) -> None: ...

    def gemini(self, agent_id: str) -> str | None: ...

    def remember_pending(self, agent_id: str, public: PublicKey) -> None: ...

    def take_pending(self, agent_id: str) -> PublicKey | None: ...

    def forget_agent(self, agent_id: str, names: Iterable[str]) -> None:
        """Delete the agent's signing key, grant, Gemini key, and the named secrets."""
        ...


FIXED_SECRETS = ("signing-key", "grant", "gemini")


class KeyRing:
    """Public keys accepted for an agent. Overlap is 10 minutes on the injected clock."""

    def __init__(self) -> None:
        self._current: dict[str, str] = {}
        self._previous: dict[str, tuple[str, datetime]] = {}

    def current_x(self, agent_id: str) -> str | None:
        return self._current.get(agent_id)

    def install(self, agent_id: str, public: PublicKey, *, now: datetime) -> None:
        del now
        self._current.setdefault(agent_id, public.to_jwk()["x"])

    def rotate(self, agent_id: str, public: PublicKey, *, now: datetime) -> None:
        current = self._current.get(agent_id)
        if current is None:
            raise CapError("KEY_UNKNOWN", agent_id)
        nxt = public.to_jwk()["x"]
        self._previous[agent_id] = (current, now + OVERLAP)
        self._current[agent_id] = nxt

    def accepts(self, agent_id: str, public: PublicKey, *, now: datetime) -> bool:
        x = public.to_jwk()["x"]
        if self._current.get(agent_id) == x:
            return True
        previous = self._previous.get(agent_id)
        if previous is None:
            return False
        old, until = previous
        return old == x and now < until


class SecretBox:
    def __init__(self) -> None:
        self._values: dict[tuple[str, str], str] = {}
        self._agent_keys: dict[str, list[PrivateKey]] = {}
        self._pending: dict[str, PublicKey] = {}

    def hold_agent_key(self, agent_id: str, private: PrivateKey) -> None:
        self._agent_keys.setdefault(agent_id, []).append(private)

    def agent_key(self, agent_id: str) -> PrivateKey:
        found = self._agent_keys.get(agent_id)
        if not found:
            raise CapError("KEY_UNKNOWN", agent_id)
        return found[-1]

    def pod_material(self, agent_id: str) -> Mapping[str, str] | None:
        found = self._agent_keys.get(agent_id)
        grant = self._values.get((agent_id, "grant"))
        if not found or not grant:
            return None
        return {"key": found[-1].to_pem(), "grant": grant}

    def set_secret(self, agent_id: str, name: str, value: str) -> None:
        if name == "gemini":
            raise CapError("POLICY_VIOLATION", "gemini keys are city managed")
        self._values[(agent_id, name)] = value

    def issue_llm(self, agent_id: str) -> None:
        key = (agent_id, "gemini")
        if key not in self._values:
            self._values[key] = ephemeral_secret()

    def rotate_llm(self, agent_id: str) -> None:
        self._values[(agent_id, "gemini")] = ephemeral_secret()

    def revoke_llm(self, agent_id: str) -> None:
        self._values.pop((agent_id, "gemini"), None)

    def gemini(self, agent_id: str) -> str | None:
        return self._values.get((agent_id, "gemini"))

    def remember_pending(self, agent_id: str, public: PublicKey) -> None:
        self._pending[agent_id] = public

    def take_pending(self, agent_id: str) -> PublicKey | None:
        return self._pending.pop(agent_id, None)

    def forget_agent(self, agent_id: str, names: Iterable[str]) -> None:
        self._agent_keys.pop(agent_id, None)
        self._pending.pop(agent_id, None)
        for name in {*FIXED_SECRETS, *names}:
            self._values.pop((agent_id, name), None)


def ephemeral_secret() -> str:
    """Opaque secret. Callers must not log or return the result."""
    return token_hex(32)
