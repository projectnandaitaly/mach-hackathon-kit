"""JSON logs with redaction and a per-second drop counter."""

from __future__ import annotations

from datetime import datetime
from typing import IO, Any

from city_sdk.utils.logging import ExcInfo, LogLimiter, build_line, format_exception, write_line


class HarnessLog:
    def __init__(
        self,
        *,
        per_second: int = 50,
        burst: int | None = None,
        now: datetime | None = None,
        stream: IO[str] | None = None,
        console: bool = False,
    ) -> None:
        self.per_second = per_second
        self.lines: list[dict[str, Any]] = []
        self._limiter = LogLimiter(per_second=per_second, burst=burst, now=now)
        self._stream = stream
        self._console = console

    @property
    def dropped(self) -> int:
        return self._limiter.dropped

    def info(self, message: str, *, now: datetime, **fields: object) -> None:
        self._emit("INFO", message, now=now, fields=fields)

    def error(
        self,
        message: str,
        *,
        now: datetime,
        exc_info: ExcInfo = None,
        **fields: object,
    ) -> None:
        self._emit("ERROR", message, now=now, fields=fields, exc_info=exc_info)

    def _emit(
        self,
        severity: str,
        message: str,
        *,
        now: datetime,
        fields: dict[str, object],
        exc_info: ExcInfo = None,
    ) -> None:
        if not self._limiter.allow(now):
            return
        line = build_line(
            severity,
            message,
            now=now,
            fields=fields,
            dropped=self._limiter.dropped,
            exception=format_exception(exc_info),
        )
        self.lines.append(line)
        if self._stream is not None:
            write_line(line, self._stream, console=self._console)
