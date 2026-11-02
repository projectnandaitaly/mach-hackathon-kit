"""Role families and their grant permissions (ADR-044, spec 21 §21.10)."""

from __future__ import annotations

from collections.abc import Mapping

FAMILIES = ("booking_centre", "gp", "hospital", "specialist_care", "family_counseling")

_BASE = frozenset({"invoke", "rate", "emergency.escalate"})
_FACILITY = _BASE | {"ledger.write:bookings", "ledger.write:checkins"}
_PERMISSIONS: dict[str, frozenset[str]] = {
    "booking_centre": _BASE,
    "gp": _BASE | {"ledger.write:prescriptions"},
    "hospital": _FACILITY,
    "family_counseling": _FACILITY,
    "specialist_care": _FACILITY | {"ledger.write:reports", "ledger.write:prescriptions"},
}


def family_of(card: Mapping[str, object]) -> str | None:
    """The card's role family, or None for a role outside the five (``b2b``)."""

    role = card.get("role")
    if role in {"booking_centre", "gp"}:
        return str(role)
    if role != "facility":
        return None
    raw = card.get("service_types")
    types = [item for item in raw if isinstance(item, str)] if isinstance(raw, list) else []
    for family in ("specialist_care", "family_counseling"):
        if any(item.startswith(family + ".") for item in types):
            return family
    return "hospital"


def role_permissions(family: str) -> frozenset[str]:
    return _PERMISSIONS[family]
