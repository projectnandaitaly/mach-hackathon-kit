"""City-minted agent ids, one per (team, family) (ADR-085).

An id is ``agent:<rid>``. ``rid`` is random, so the id says nothing about the team or the
role. The pair keeps its id across deploys and rollbacks. A deleted id is never issued
again; the pair's next deploy gets a new one.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from cap.core.errors import CapError
from city_index.families import FAMILIES, family_of

ID_FAMILIES = (*FAMILIES, "b2b")
_CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"
RID_LENGTH = 12
MINTED = re.compile(r"^agent:[0-9a-hjkmnp-tv-z]{12}$")


def new_rid(choice: Callable[[str], str] = secrets.choice) -> str:
    return "".join(choice(_CROCKFORD) for _ in range(RID_LENGTH))


def id_name(agent_id: str) -> str:
    """The id's last segment: the rid of a minted id, the slug of ``agent:<team>/<slug>``."""

    return agent_id.removeprefix("agent:").rsplit("/", 1)[-1]


def id_family(card: Mapping[str, object]) -> str:
    """The family a card's id is minted for. ``b2b`` for a card outside the five."""

    family = family_of(card)
    if family is not None:
        return family
    if card.get("role") == "b2b":
        return "b2b"
    raise CapError("SCHEMA_INVALID", "role")


@dataclass
class AgentIds:
    pairs: dict[tuple[str, str], str] = field(default_factory=dict)
    retired: set[str] = field(default_factory=set)
    rid: Callable[[], str] = new_rid

    def assign(self, team_id: str, family: str) -> str:
        """The pair's live id, minted on first use."""

        if family not in ID_FAMILIES:
            raise CapError("SCHEMA_INVALID", "family")
        found = self.pairs.get((team_id, family))
        if found is not None:
            return found
        taken = set(self.pairs.values()) | self.retired
        for _ in range(8):
            agent_id = f"agent:{self.rid()}"
            if agent_id not in taken:
                self.pairs[(team_id, family)] = agent_id
                return agent_id
        raise CapError("DUPLICATE", "agent id")

    def reserve(self, team_id: str, family: str, agent_id: str) -> None:
        """Bind a city-chosen id. Refused when another pair holds it or it was deleted."""

        holder = self.team_of(agent_id)
        current = self.pairs.get((team_id, family))
        if agent_id in self.retired or (holder is not None and current != agent_id):
            raise CapError("DUPLICATE", "agent id")
        if current is not None and current != agent_id:
            raise CapError("DUPLICATE", "family already has an id")
        self.pairs[(team_id, family)] = agent_id

    def get(self, team_id: str, family: str) -> str | None:
        return self.pairs.get((team_id, family))

    def team_of(self, agent_id: str) -> str | None:
        for (team_id, _family), found in self.pairs.items():
            if found == agent_id:
                return team_id
        return None

    def of_team(self, team_id: str) -> dict[str, str]:
        """``family -> agent_id`` for the team's live ids."""

        return {family: found for (team, family), found in self.pairs.items() if team == team_id}

    def retire(self, agent_id: str) -> None:
        """Free the pair and never issue ``agent_id`` again."""

        self.pairs = {pair: found for pair, found in self.pairs.items() if found != agent_id}
        self.retired.add(agent_id)

    def blob(self) -> dict[str, object]:
        return {
            "pairs": [
                {"team_id": team, "family": family, "agent_id": found}
                for (team, family), found in sorted(self.pairs.items())
            ],
            "retired": sorted(self.retired),
        }

    def load(self, value: object) -> None:
        pairs: dict[tuple[str, str], str] = {}
        retired: set[str] = set()
        if isinstance(value, Mapping):
            rows = value.get("pairs")
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, Mapping):
                    continue
                team, family, found = row.get("team_id"), row.get("family"), row.get("agent_id")
                if isinstance(team, str) and isinstance(family, str) and isinstance(found, str):
                    pairs[(team, family)] = found
            gone = value.get("retired")
            retired = (
                {item for item in gone if isinstance(item, str)}
                if isinstance(gone, list)
                else set()
            )
        self.pairs = pairs
        self.retired = retired
