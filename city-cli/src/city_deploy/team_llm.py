"""Team gateway virtual keys for Deploy login download."""

from __future__ import annotations

from secrets import token_hex
from typing import Protocol


class TeamKeysPort(Protocol):
    def ensure_team(self, team_id: str) -> str: ...

    def read_team(self, team_id: str) -> str | None: ...

    def revoke_team(self, team_id: str) -> bool:
        """Delete the team key. True when one existed."""
        ...


class MemoryTeamKeys:
    """In-process keys for local Deploy / tests."""

    def __init__(self) -> None:
        self._keys: dict[str, str] = {}

    def ensure_team(self, team_id: str) -> str:
        slug = team_id.strip()
        found = self._keys.get(slug)
        if found is not None:
            return found
        value = token_hex(32)
        self._keys[slug] = value
        return value

    def read_team(self, team_id: str) -> str | None:
        return self._keys.get(team_id.strip())

    def revoke_team(self, team_id: str) -> bool:
        return self._keys.pop(team_id.strip(), None) is not None

    def ensure_teams(self, team_ids: list[str]) -> dict[str, int]:
        issued = 0
        already = 0
        for team_id in team_ids:
            before = self.read_team(team_id)
            self.ensure_team(team_id)
            if before is None:
                issued += 1
            else:
                already += 1
        return {"issued": issued, "already": already, "failed": 0}
