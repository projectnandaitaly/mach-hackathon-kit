"""Conformance profiles from specification 09 §16.1. Stages 25 and 26 stay deferred."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol, cast

import httpx
from cap.core.errors import CapError
from city_conform.agent import SuiteSigners
from city_conform.remote import CityEvidence, HttpCityEvidence
from city_conform.runner import Report, Stage, run_profile
from city_conform.stages import CATALOG
from city_conform.world import good_agent
from city_sdk.ports import FixedClock

FAST_STAGES = frozenset({1, 2, 3, 4, 5, 8, 10, 12, 16, 17, 20, 25, 26})
_REFERENCE_AT = datetime(2026, 11, 21, 10, 14, tzinfo=UTC)


def select_profile(profile: str) -> tuple[Stage, ...]:
    if profile == "full":
        return CATALOG
    if profile == "fast":
        return tuple(stage for stage in CATALOG if stage.number in FAST_STAGES)
    raise CapError("SCHEMA_INVALID", "profile")


class Suite(Protocol):
    async def run(self, *, card: Mapping[str, Any], profile: str) -> Report: ...


class ReferenceSuite:
    """The city's reference agent. Team brains are not loaded in CI."""

    async def run(self, *, card: Mapping[str, Any], profile: str) -> Report:
        del card
        agent = await good_agent(clock=FixedClock(_REFERENCE_AT))
        return await run_profile(agent, select_profile(profile))


class CandidateSuite:
    """Probes the candidate URL and the city evidence that runtime recorded."""

    def __init__(
        self,
        runtime: Any,
        *,
        evidence: CityEvidence | None = None,
        signers: SuiteSigners | None = None,
    ) -> None:
        self._runtime = runtime
        self._evidence = evidence
        self._signers = signers

    async def run(self, *, card: Mapping[str, Any], profile: str) -> Report:
        from city_conform.remote import conform_url

        agent_id = str(card["agent_id"])
        url = self._runtime.candidate_url(agent_id)
        evidence = (
            self._evidence if self._evidence is not None else _evidence_for(self._runtime, agent_id)
        )
        signers = self._signers if self._signers is not None else _signers_for(self._runtime)
        transport = _transport_for(self._runtime)
        return await conform_url(
            url,
            stages=select_profile(profile),
            transport=transport,
            evidence=evidence,
            signers=signers,
            index_url=_index_url_for(self._runtime, agent_id),
            deliveries=_deliveries_for(self._runtime),
        )


def _deliveries_for(runtime: Any) -> Any:
    opener = getattr(runtime, "open_conformance", None)
    return opener if callable(opener) else None


def _index_url_for(runtime: Any, agent_id: str) -> str | None:
    locate = getattr(runtime, "index_url", None)
    if not callable(locate):
        return None
    url = locate(agent_id)
    return url if isinstance(url, str) and url else None


def _evidence_for(runtime: Any, agent_id: str) -> CityEvidence | None:
    direct = getattr(runtime, "city_evidence", None)
    if callable(direct):
        found = direct(agent_id)
        if found is not None:
            return cast(CityEvidence, found)
    locate = getattr(runtime, "evidence_url", None)
    if not callable(locate):
        return None
    url = locate(agent_id)
    if not isinstance(url, str) or not url:
        return None
    return HttpCityEvidence(url, agent_id=agent_id)


def _signers_for(runtime: Any) -> SuiteSigners | None:
    provide = getattr(runtime, "suite_keys", None)
    if not callable(provide):
        return None
    found = provide()
    if isinstance(found, SuiteSigners):
        return found
    return None


def _transport_for(runtime: Any) -> httpx.AsyncBaseTransport | None:
    transport = getattr(runtime, "transport", None)
    if isinstance(transport, httpx.AsyncBaseTransport):
        return transport
    return None
