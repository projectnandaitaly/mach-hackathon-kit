"""Resolve the City Deploy / platform base URL for the CLI and skeleton."""

from __future__ import annotations

from collections.abc import Mapping

# Live Deploy API on the city edge (ADR-023). Override with CITY_PLATFORM_URL /
# CITY_DEPLOY_URL, or pass --api-url. Use "local" for the in-process Deploy service.
DEFAULT_CITY_PLATFORM_URL = "https://deploy.projectnandaitaly.ai"
_LOCAL = frozenset({"local", "inprocess", "memory"})


def resolve_platform_url(
    explicit: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Return a remote base URL, or ``None`` for the in-process Deploy service.

    Precedence: ``explicit`` (--api-url) → ``CITY_DEPLOY_URL`` → ``CITY_PLATFORM_URL``
    → ``DEFAULT_CITY_PLATFORM_URL``. The tokens ``local``, ``inprocess``, and
    ``memory`` select in-process mode.
    """
    source = env if env is not None else {}
    if explicit is not None:
        raw = explicit.strip()
    else:
        raw = (
            source.get("CITY_DEPLOY_URL", "").strip()
            or source.get("CITY_PLATFORM_URL", "").strip()
            or DEFAULT_CITY_PLATFORM_URL
        )
    if raw.lower() in _LOCAL:
        return None
    return raw.rstrip("/") or None
