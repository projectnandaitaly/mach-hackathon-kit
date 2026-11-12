"""Live citizen keys from Deploy-issued per-wave grants (ADR-039)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Protocol

from cap.core.errors import CapError
from cap.core.keys import PublicKey
from cap.core.timeutil import parse_time

from city_index.ports import Clock

CITIZEN = re.compile(r"^citizen:[A-Z0-9-]+$")


def is_citizen(party: str) -> bool:
    return CITIZEN.fullmatch(party) is not None


class CitizenGrantSource(Protocol):
    def citizen_grant(self, citizen_id: str) -> dict[str, Any] | None: ...


class CitizenKeys:
    """A citizen kid is ``<citizen_id>#<wave_id>`` and resolves while its grant is live."""

    def __init__(self, grants: CitizenGrantSource, clock: Clock) -> None:
        self._grants = grants
        self._clock = clock

    def live(self, citizen_id: str) -> bool:
        return self._live_grant(citizen_id) is not None

    def refresh(self, citizen_id: str) -> None:
        """Drop a cached grant so the next read sees a key a new holder just published."""

        forget = getattr(self._grants, "forget_citizen", None)
        if callable(forget):
            forget(citizen_id)

    def key(self, kid: str, permission: str) -> PublicKey:
        citizen_id, sep, wave = kid.partition("#")
        if not sep or not is_citizen(citizen_id):
            raise KeyError(kid)
        grant = self._live_grant(citizen_id)
        if grant is None or grant.get("wave_id") != wave:
            raise KeyError(kid)
        permissions = grant.get("permissions")
        if not isinstance(permissions, list) or permission not in permissions:
            raise KeyError(kid)
        jwk = grant.get("citizen_pubkey")
        if not isinstance(jwk, Mapping):
            raise KeyError(kid)
        return PublicKey.from_jwk(dict(jwk))

    def _live_grant(self, citizen_id: str) -> Mapping[str, Any] | None:
        grant = self._grants.citizen_grant(citizen_id)
        if grant is None:
            return None
        try:
            not_before = parse_time(grant.get("not_before"))
            not_after = parse_time(grant.get("not_after"))
        except CapError:
            return None
        now = self._clock.now()
        if not not_before <= now <= not_after:
            return None
        return grant
