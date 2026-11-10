"""Natural-language search (ADR-110): free text to role families, never to a ranking.

A fixed keyword table reads the text first. When it finds no family and a resolver is
configured, the resolver may name families from the same closed set. Text that names
nothing reads as every role.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from cap.core.errors import CapError

from city_index.families import FAMILIES

LEDGER = "city:ledger"
SEARCH_FAMILIES = (*FAMILIES, "ledger")
MAX_QUERY = 500

_FACILITY = ("facility", "facilities", "struttura", "strutture", "provider", "erogatore")
_KEYWORDS: dict[str, tuple[str, ...]] = {
    "booking_centre": (
        "booking centre",
        "booking center",
        "booking_centre",
        "cup",
        "centro prenotazioni",
        "call centre",
        "call center",
    ),
    "gp": (
        "gp",
        "general practitioner",
        "family doctor",
        "medico di base",
        "medico di famiglia",
        "medico curante",
        "primary care",
    ),
    "hospital": (
        *_FACILITY,
        "hospital",
        "ospedal",
        "clinic",
        "clinica",
        "emergency room",
        "pronto soccorso",
        "radiolog",
        "surgery",
        "chirurg",
    ),
    "specialist_care": (
        *_FACILITY,
        "specialist",
        "specialis",
        "ambulatori",
        "outpatient",
        "physiotherap",
        "fisioterap",
        "cardiolog",
        "dermatolog",
        "ophthalmolog",
        "oculist",
    ),
    "family_counseling": (
        *_FACILITY,
        "family counseling",
        "family counselling",
        "family_counseling",
        "consultori",
        "counseling",
        "counselling",
        "pregnan",
        "gravidanz",
        "contracept",
        "contraccez",
        "gynecolog",
        "ginecolog",
    ),
    "ledger": ("ledger", "register", "registro", "registri"),
}

# Optional free-text reader over the same closed set; may answer any subset of it.
Resolver = Callable[[str], Sequence[str]]


def _pattern(keyword: str) -> re.Pattern[str]:
    # Short words match whole; longer ones match as a stem at a word start.
    tail = r"\b" if len(keyword) <= 4 else ""
    return re.compile(r"\b" + re.escape(keyword) + tail)


_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    family: tuple(_pattern(word) for word in words) for family, words in _KEYWORDS.items()
}


def families_for(query: object, resolver: Resolver | None = None) -> list[str]:
    """The families the query names, in the fixed order of ``SEARCH_FAMILIES``."""

    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY:
        raise CapError("SCHEMA_INVALID", f"query is 1 to {MAX_QUERY} characters")
    text = query.lower()
    found = {
        family
        for family, patterns in _PATTERNS.items()
        if any(pattern.search(text) for pattern in patterns)
    }
    if not found and resolver is not None:
        found = {family for family in resolver(query) if family in SEARCH_FAMILIES}
    if not found:
        found = set(FAMILIES)
    return [family for family in SEARCH_FAMILIES if family in found]


def ledger_row() -> dict[str, str]:
    return {"agent_id": LEDGER, "role": "ledger"}
