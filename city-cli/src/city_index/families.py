"""Role families and their grant permissions (ADR-044, spec 21 §21.10)."""

from __future__ import annotations

from collections.abc import Mapping

FAMILIES = ("booking_centre", "gp", "hospital", "specialist_care", "family_counseling")

# Role families a caller can be shown. The ledger follows the query text, not this table.
# The Index still returns every match and does not pick one.
COUNTERPARTS: dict[str, frozenset[str]] = {
    "gp": frozenset({"booking_centre"}),
    "booking_centre": frozenset({"gp", "hospital", "specialist_care", "family_counseling"}),
    "hospital": frozenset({"booking_centre", "specialist_care", "family_counseling"}),
    "specialist_care": frozenset({"booking_centre", "gp", "hospital", "family_counseling"}),
    "family_counseling": frozenset({"booking_centre", "hospital", "specialist_care"}),
}

_BASE = frozenset({"invoke", "rate", "emergency.escalate"})
_FACILITY = _BASE | {"ledger.write:bookings", "ledger.write:checkins"}
_PERMISSIONS: dict[str, frozenset[str]] = {
    "booking_centre": _BASE,
    "gp": _BASE | {"ledger.write:prescriptions"},
    "hospital": _FACILITY,
    "family_counseling": _FACILITY,
    "specialist_care": _FACILITY | {"ledger.write:reports", "ledger.write:prescriptions"},
}


def card_capabilities(card: Mapping[str, object]) -> list[str]:
    """Capability strings on a card.

    Cards written before the rename still store the list under ``service_types``.
    A card that has ``capabilities`` uses that list, including when it is empty.
    """

    raw = card.get("capabilities")
    if not isinstance(raw, list):
        raw = card.get("service_types")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str)]


def family_of(card: Mapping[str, object]) -> str | None:
    """The card's role family, or None for a role outside the five (``b2b``)."""

    role = card.get("role")
    if role in {"booking_centre", "gp"}:
        return str(role)
    if role != "facility":
        return None
    types = card_capabilities(card)
    for family in ("specialist_care", "family_counseling"):
        if any(item.startswith(family + ".") for item in types):
            return family
    return "hospital"


def role_permissions(family: str) -> frozenset[str]:
    return _PERMISSIONS[family]
