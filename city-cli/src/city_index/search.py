"""Natural-language search (ADR-110, ADR-114): free text to candidates, never a ranking.

A fixed keyword table reads the text first. When it finds no family and a resolver is
configured, the resolver may name families from the same closed set. Text that names
nothing reads as every role. Search then keeps an agent when its role is one the
caller can use and, when the text names a capability, that capability is on its card.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence

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


def named_families(query: object, resolver: Resolver | None = None) -> set[str]:
    """Families the text names. Empty when it names none. Does not default to every role."""

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
    return found


def families_for(query: object, resolver: Resolver | None = None) -> list[str]:
    """The families the query names, in the fixed order of ``SEARCH_FAMILIES``."""

    found = named_families(query, resolver)
    if not found:
        found = set(FAMILIES)
    return [family for family in SEARCH_FAMILIES if family in found]


def mentioned_capabilities(text: str, capabilities: Iterable[str]) -> set[str]:
    """Capability suffixes the text names, such as ``cardiology`` or ``gp``."""

    words = set(re.findall(r"[a-z0-9_]+", text.lower()))
    found: set[str] = set()
    for capability in capabilities:
        suffix = capability.lower().rsplit(".", 1)[-1]
        if suffix in words:
            found.add(suffix)
            continue
        if len(suffix) < 5:
            continue
        if any(len(word) >= 5 and suffix.startswith(word) for word in words):
            found.add(suffix)
    return found


def capability_fits(capabilities: Iterable[str], mentioned: set[str]) -> bool:
    """True when ``mentioned`` is empty or one of ``capabilities`` is in it."""

    if not mentioned:
        return True
    return any(item.lower().rsplit(".", 1)[-1] in mentioned for item in capabilities)


def ledger_row() -> dict[str, str]:
    return {"agent_id": LEDGER, "role": "ledger"}
