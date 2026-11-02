"""Structured agent logs. Cloud Logging gets one JSON line; a local console gets a block."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import traceback
from collections.abc import Mapping
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from types import TracebackType
from typing import IO, Any

ExcInfo = (
    BaseException | bool | tuple[type[BaseException], BaseException, TracebackType | None] | None
)

_BEARER = re.compile(r"Bearer\s+\S+", re.IGNORECASE)
_KEY_MARKER = "BEGIN" + " PRIVATE KEY"
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_FISCAL_CODE = re.compile(r"\b[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]\b")
_PHONE = re.compile(r"(?:\+39|0039)\s?\d{6,12}")
_GRANT = re.compile(r"\bgrant=\S+", re.IGNORECASE)
_GEMINI = re.compile(r"\bgemini=\S+", re.IGNORECASE)

_CONTEXT: ContextVar[dict[str, str]] = ContextVar("city_log_context", default={})
_STANDARD = (
    "agent_id",
    "team_id",
    "version",
    "instance",
    "market_id",
    "msg_id",
    "need_id",
    "conversation_id",
    "fence",
    "attempt",
    "trace_id",
    "span_id",
    "category",
)


def redact(text: str) -> str:
    """Mask secrets and synthetic personal data before a line is written."""
    cleaned = _BEARER.sub("Bearer ***", text)
    cleaned = cleaned.replace(_KEY_MARKER, "***")
    cleaned = _GRANT.sub("grant=***", cleaned)
    cleaned = _GEMINI.sub("gemini=***", cleaned)
    cleaned = _EMAIL.sub("***", cleaned)
    cleaned = _FISCAL_CODE.sub("***", cleaned)
    return _PHONE.sub("***", cleaned)


class LogLimiter:
    """Token bucket. ``burst`` defaults to the per-second rate."""

    def __init__(
        self,
        *,
        per_second: int = 50,
        burst: int | None = None,
        now: datetime | None = None,
    ) -> None:
        self.per_second = per_second
        self.burst = per_second if burst is None else burst
        self.dropped = 0
        self._tokens = float(self.burst)
        self._window = now

    def allow(self, now: datetime) -> bool:
        if self._window is not None:
            elapsed = (now - self._window).total_seconds()
            if elapsed > 0:
                self._tokens = min(self.burst, self._tokens + elapsed * self.per_second)
        self._window = now
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        self.dropped += 1
        return False


def bind(**fields: object) -> Token[dict[str, str]]:
    """Attach harness correlation fields to every log emitted in this context."""
    current = dict(_CONTEXT.get())
    for key, value in fields.items():
        if value is None:
            current.pop(key, None)
        elif isinstance(value, str):
            current[key] = redact(value)
        else:
            current[key] = str(value)
    return _CONTEXT.set(current)


def reset(token: Token[dict[str, str]]) -> None:
    _CONTEXT.reset(token)


def format_exception(exc_info: ExcInfo = None) -> str | None:
    """Return a redacted traceback for an active or passed exception."""
    if exc_info is None or exc_info is False:
        return None
    if exc_info is True:
        info = sys.exc_info()
        if info[0] is None:
            return None
        text = "".join(traceback.format_exception(*info))
    elif isinstance(exc_info, BaseException):
        text = "".join(traceback.format_exception(type(exc_info), exc_info, exc_info.__traceback__))
    elif isinstance(exc_info, tuple) and len(exc_info) == 3:
        if exc_info[0] is None:
            return None
        text = "".join(traceback.format_exception(*exc_info))
    else:
        return None
    return redact(text.rstrip("\n"))


def build_line(
    severity: str,
    message: str,
    *,
    now: datetime,
    fields: Mapping[str, object] | None = None,
    dropped: int = 0,
    exception: str | None = None,
) -> dict[str, Any]:
    line: dict[str, Any] = {
        "severity": severity,
        "message": redact(message),
        "time": now.astimezone(UTC).isoformat(),
        "claim": True,
    }
    merged: dict[str, object] = dict(_CONTEXT.get())
    if fields:
        merged.update(fields)
    for key, value in merged.items():
        if key in {"exception", "exc_info"}:
            continue
        if key == "trace_id" and isinstance(value, str) and value:
            line["logging.googleapis.com/trace"] = value
        line[key] = redact(value) if isinstance(value, str) else value
    if dropped:
        line["log.dropped"] = dropped
    stack = exception
    if stack is None and fields:
        raw = fields.get("exception")
        if isinstance(raw, str) and raw:
            stack = redact(raw)
    if stack:
        line["exception"] = stack
    return line


_DETAIL = (
    ("agent_id", "agent"),
    ("team_id", "team"),
    ("version", "version"),
    ("msg_id", "msg"),
    ("need_id", "need"),
    ("conversation_id", "conversation"),
    ("market_id", "market"),
    ("instance", "instance"),
    ("log.dropped", "dropped"),
)
_LEVEL = {"WARNING": "WARN", "CRITICAL": "CRIT"}
_SEVERITY_COLOR = {
    "DEBUG": "2",
    "INFO": "36",
    "WARN": "33",
    "WARNING": "33",
    "ERROR": "31",
    "CRIT": "35",
    "CRITICAL": "35",
}


def render_console(
    line: Mapping[str, object],
    *,
    color: bool = False,
    width: int | None = None,
) -> str:
    """Local block. Fields wrap only between keys, so an id is never split."""
    stamp = str(line.get("time", ""))
    clock = stamp[11:19] if len(stamp) >= 19 else stamp
    severity = str(line.get("severity", "INFO"))
    level = f"{_LEVEL.get(severity, severity):<5}"
    category = str(line.get("category", "") or "")
    message = str(line.get("message", "")).replace("\n", " ").strip()
    if message == category:
        message = ""
    prefix = f"{clock} {level} "
    tag = f"[{category}] " if category else ""
    columns = width if width is not None else _console_width()
    message_rows = _wrap_words(message, max(16, columns - len(prefix) - len(tag)))
    head = _paint(clock, "2", color) + " "
    head += _paint(level, _SEVERITY_COLOR.get(level.strip(), "36"), color) + " "
    head += _paint(tag, "2", color) + (message_rows[0] if message_rows else "")
    rows = [head.rstrip()]
    indent = " " * len(prefix)
    rows.extend(indent + part for part in message_rows[1:])
    rows.extend(_pack_fields(_details(line), columns, len(prefix)))
    exception = line.get("exception")
    if isinstance(exception, str) and exception:
        for part in exception.splitlines() or [exception]:
            rows.append(indent + part)
    return "\n".join(rows)


def _details(line: Mapping[str, object]) -> list[str]:
    fields = [f"{label}={line[key]}" for key, label in _DETAIL if line.get(key)]
    trace = line.get("logging.googleapis.com/trace")
    if trace:
        fields.append(f"trace={trace}")
    return fields


def _wrap_words(text: str, width: int) -> list[str]:
    words = [word for word in text.split(" ") if word]
    rows: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if current and len(candidate) > width:
            rows.append(current)
            current = word
        else:
            current = candidate
    if current:
        rows.append(current)
    return rows


def _pack_fields(fields: list[str], width: int, indent: int) -> list[str]:
    room = max(8, width - indent)
    rows: list[str] = []
    current: list[str] = []
    used = 0
    for field in fields:
        extra = len(field) if not current else len(field) + 2
        if current and used + extra > room:
            rows.append(" " * indent + "  ".join(current))
            current = [field]
            used = len(field)
        else:
            current.append(field)
            used += extra
    if current:
        rows.append(" " * indent + "  ".join(current))
    return rows


def _console_width() -> int:
    try:
        columns = shutil.get_terminal_size(fallback=(100, 24)).columns
    except OSError:
        return 100
    return columns if columns >= 40 else 100


def write_line(
    line: Mapping[str, object],
    stream: IO[str] | None = None,
    *,
    console: bool = False,
) -> None:
    target = sys.stdout if stream is None else stream
    if console:
        text = render_console(line, color=_wants_color(target))
    else:
        text = json.dumps(line, default=str)
    target.write(text + "\n")
    target.flush()


def _paint(text: str, code: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"\033[{code}m{text}\033[0m"


def _wants_color(stream: IO[str]) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    check = getattr(stream, "isatty", None)
    return bool(check and check())


def line_from_record(record: logging.LogRecord) -> dict[str, Any]:
    now = datetime.fromtimestamp(record.created, UTC)
    fields: dict[str, object] = {
        key: getattr(record, key) for key in _STANDARD if getattr(record, key, None) is not None
    }
    extra = getattr(record, "city_fields", None)
    if isinstance(extra, Mapping):
        fields.update(extra)
    dropped = getattr(record, "city_dropped", 0)
    count = dropped if isinstance(dropped, int) else 0
    exception: str | None = None
    if record.exc_info and record.exc_info[0] is not None:
        exception = format_exception(record.exc_info)
    elif record.exc_text:
        exception = redact(record.exc_text.rstrip("\n"))
    return build_line(
        record.levelname,
        record.getMessage(),
        now=now,
        fields=fields,
        dropped=count,
        exception=exception,
    )


class CityJsonFormatter(logging.Formatter):
    """Format a stdlib record as one Cloud Logging JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(line_from_record(record), default=str)


class CityConsoleFormatter(logging.Formatter):
    """Format a stdlib record as a local, structured block."""

    def format(self, record: logging.LogRecord) -> str:
        return render_console(line_from_record(record), color=_wants_color(sys.stdout))


class CityRateFilter(logging.Filter):
    def __init__(self, limiter: LogLimiter | None = None) -> None:
        super().__init__()
        self.limiter = LogLimiter() if limiter is None else limiter

    def filter(self, record: logging.LogRecord) -> bool:
        now = datetime.fromtimestamp(record.created, UTC)
        if self.limiter.allow(now):
            if self.limiter.dropped:
                record.city_dropped = self.limiter.dropped
            return True
        return False


def uvicorn_log_config(*, console: bool = False) -> dict[str, Any]:
    """Dict-config so Uvicorn, FastAPI, and team loggers share the city formatter."""
    formatter = (
        "city_sdk.utils.logging.CityConsoleFormatter"
        if console
        else "city_sdk.utils.logging.CityJsonFormatter"
    )
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"city": {"()": formatter}},
        "filters": {"city_rate": {"()": "city_sdk.utils.logging.CityRateFilter"}},
        "handlers": {
            "city": {
                "class": "logging.StreamHandler",
                "formatter": "city",
                "filters": ["city_rate"],
                "stream": "ext://sys.stdout",
            }
        },
        "loggers": {
            "uvicorn": {"handlers": ["city"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": ["city"], "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": ["city"], "level": "INFO", "propagate": False},
            "fastapi": {"handlers": ["city"], "level": "INFO", "propagate": False},
            "city": {"handlers": ["city"], "level": "INFO", "propagate": False},
        },
        "root": {"handlers": ["city"], "level": "INFO"},
    }


def configure(*, console: bool = False, level: int = logging.INFO) -> logging.Logger:
    """Install the city handler on the ``city`` logger and the root logger."""
    formatter: logging.Formatter = CityConsoleFormatter() if console else CityJsonFormatter()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(CityRateFilter())
    city = logging.getLogger("city")
    city.handlers.clear()
    city.setLevel(level)
    city.propagate = False
    city.addHandler(handler)
    root = logging.getLogger()
    root.handlers = [
        item
        for item in root.handlers
        if not isinstance(item.formatter, (CityJsonFormatter, CityConsoleFormatter))
    ]
    root.addHandler(handler)
    root.setLevel(level)
    return city


def get_logger(name: str = "city") -> logging.Logger:
    if not logging.getLogger("city").handlers:
        configure()
    return logging.getLogger(name)
