"""Stage machine. A blocking failure skips the rest. A crash is the platform's."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from city_conform.agent import Agent


class Outcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_TESTED = "NOT TESTED"
    INCONCLUSIVE = "INCONCLUSIVE"
    ERROR = "ERROR"


@dataclass(frozen=True)
class Verdict:
    outcome: Outcome
    detail: str = ""


def passed() -> Verdict:
    return Verdict(Outcome.PASS)


def failed(detail: str) -> Verdict:
    return Verdict(Outcome.FAIL, detail)


def skipped(detail: str) -> Verdict:
    return Verdict(Outcome.NOT_TESTED, detail)


def inconclusive(detail: str) -> Verdict:
    return Verdict(Outcome.INCONCLUSIVE, detail)


@dataclass(frozen=True)
class Stage:
    number: int
    name: str
    blocking: bool
    fn: Callable[[Agent], Awaitable[Verdict]]


@dataclass(frozen=True)
class StageRow:
    number: int
    name: str
    blocking: bool
    outcome: Outcome
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "number": self.number,
            "name": self.name,
            "outcome": self.outcome.value,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Report:
    rows: tuple[StageRow, ...]

    @property
    def passed(self) -> bool:
        for row in self.rows:
            if row.outcome is Outcome.ERROR:
                return False
            if row.outcome is Outcome.FAIL and row.blocking:
                return False
        return True


async def run_profile(agent: Agent, stages: Sequence[Stage]) -> Report:
    blocked = False
    rows: list[StageRow] = []
    for stage in stages:
        if blocked:
            rows.append(
                StageRow(
                    stage.number,
                    stage.name,
                    stage.blocking,
                    Outcome.NOT_TESTED,
                    "blocked by an earlier failure",
                )
            )
            continue
        try:
            verdict = await stage.fn(agent)
        except Exception as exc:
            rows.append(
                StageRow(
                    stage.number,
                    stage.name,
                    stage.blocking,
                    Outcome.ERROR,
                    type(exc).__name__,
                )
            )
            blocked = True
            continue
        rows.append(
            StageRow(stage.number, stage.name, stage.blocking, verdict.outcome, verdict.detail)
        )
        if verdict.outcome is Outcome.FAIL and stage.blocking:
            blocked = True
        if verdict.outcome is Outcome.ERROR:
            blocked = True
    return Report(tuple(rows))


async def run_one(agent: Agent, stage: Stage) -> StageRow:
    report = await run_profile(agent, (stage,))
    return report.rows[0]
