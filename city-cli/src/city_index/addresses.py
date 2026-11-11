"""Milan civic addresses for an ASST (ADR-112).

The book is a snapshot of Comune di Milano DS634. A door is a pure function of
the ASST and a slot (a role family, or ``citizen:<n>``), so the map, the team
page, and the persona agree without a geocoder.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import TypedDict

from city_index.families import FAMILIES

ZONE_OF = {
    "asst-nord": "nord",
    "asst-est": "est",
    "asst-sud": "sud",
    "asst-ovest": "ovest",
    "asst-centro": "centro",
    "asst-brera": "brera",
    "asst-isola": "isola",
    "asst-navigli": "navigli",
    "asst-porta-romana": "porta-romana",
    "asst-citta-studi": "citta-studi",
    "asst-lambrate": "lambrate",
    "asst-bicocca": "bicocca",
    "asst-niguarda": "niguarda",
    "asst-bovisa": "bovisa",
    "asst-san-siro": "san-siro",
    "asst-baggio": "baggio",
    "asst-lorenteggio": "lorenteggio",
    "asst-giambellino": "giambellino",
    "asst-barona": "barona",
    "asst-corvetto": "corvetto",
    "asst-rogoredo": "rogoredo",
    "asst-forlanini": "forlanini",
    "asst-ortica": "ortica",
    "asst-greco": "greco",
    "asst-gorla": "gorla",
    "asst-precotto": "precotto",
    "asst-turro": "turro",
    "asst-quarto-oggiaro": "quarto-oggiaro",
    "asst-gallaratese": "gallaratese",
    "asst-portello": "portello",
    "asst-tortona": "tortona",
    "asst-chiaravalle": "chiaravalle",
    "asst-ref": "duomo",
}


class CivicAddress(TypedDict):
    label: str
    lon: float
    lat: float


# Palazzo Marino, the city hall. DS634 applied civic, edition 2026-10-01.
# The thinned zone book does not keep this door, so the Ledger is fixed here.
LEDGER_ADDRESS: CivicAddress = {
    "label": "Piazza della Scala 2",
    "lon": 9.190388,
    "lat": 45.46676,
}


def ledger_address() -> CivicAddress:
    """The Ledger's door. It does not move with an ASST."""

    return LEDGER_ADDRESS


Door = tuple[str, float, float]


def _parse(raw: object) -> dict[str, tuple[Door, ...]]:
    if not isinstance(raw, dict):
        raise ValueError("civic book")
    zones = raw.get("zones")
    if not isinstance(zones, dict) or not zones:
        raise ValueError("civic book")
    book: dict[str, tuple[Door, ...]] = {}
    for key, rows in zones.items():
        if not isinstance(key, str) or not isinstance(rows, list) or not rows:
            raise ValueError("civic book")
        parsed: list[Door] = []
        for row in rows:
            if not isinstance(row, list) or len(row) != 3:
                raise ValueError("civic book")
            label, lon, lat = row[0], row[1], row[2]
            if not isinstance(label, str) or not label:
                raise ValueError("civic book")
            if isinstance(lon, bool) or isinstance(lat, bool):
                raise ValueError("civic book")
            if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
                raise ValueError("civic book")
            parsed.append((label, float(lon), float(lat)))
        book[key] = tuple(parsed)
    return book


@lru_cache(maxsize=1)
def _book() -> dict[str, tuple[Door, ...]]:
    path = Path(__file__).with_name("civics.json")
    return _parse(json.loads(path.read_text(encoding="utf-8")))


@lru_cache(maxsize=64)
def _seats(asst: str) -> dict[str, int]:
    """Five distinct doors, one per role family."""

    rows = _book()[ZONE_OF[asst]]
    count = len(rows)
    used: set[int] = set()
    found: dict[str, int] = {}
    order = sorted(
        FAMILIES, key=lambda family: hashlib.sha256(f"{asst}\n{family}".encode()).digest()
    )
    for family in order:
        digest = hashlib.sha256(f"{asst}\n{family}".encode()).digest()
        index = int.from_bytes(digest[:4], "big") % count
        while index in used:
            index = (index + 1) % count
        used.add(index)
        found[family] = index
    return found


def address_for(asst: str, slot: str) -> CivicAddress | None:
    """The door for a role family or for ``citizen:<n>``. None when the ASST has no zone."""

    zone = ZONE_OF.get(asst)
    if zone is None:
        return None
    rows = _book().get(zone)
    if not rows:
        return None
    seats = _seats(asst)
    if slot in seats:
        index = seats[slot]
    elif slot.startswith("citizen:"):
        tail = slot.removeprefix("citizen:")
        if not tail.isdigit():
            return None
        digest = hashlib.sha256(f"{asst}\n{slot}".encode()).digest()
        index = int.from_bytes(digest[:4], "big") % len(rows)
        taken = set(seats.values())
        walked = 0
        while index in taken and walked < len(rows):
            index = (index + 1) % len(rows)
            walked += 1
    else:
        return None
    label, lon, lat = rows[index]
    return {"label": label, "lon": lon, "lat": lat}
