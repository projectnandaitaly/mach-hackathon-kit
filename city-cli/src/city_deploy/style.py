"""Human CLI chrome. JSON mode stays plain; respects NO_COLOR / non-TTY."""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import IO, Literal

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

Kind = Literal["ok", "bad", "warn", "info", "pass", "fail", "run"]

_THEME = Theme(
    {
        "city": "bold cyan",
        "ok": "bold green",
        "bad": "bold red",
        "warn": "bold yellow",
        "info": "cyan",
        "mute": "dim",
        "pass": "bold green",
        "fail": "bold red",
        "run": "cyan",
        "label": "bold",
        "hero": "bold cyan",
        "rule": "dim cyan",
        # Chat streams. Light enough to read on a dark terminal, and far enough apart
        # that input, logs, and the answer stay distinct. The gutter word names the stream.
        "you": "bold #8ecbff",
        "log": "#e6c36a",
        "out": "#7dcea0",
        # Model exchange inside the log gutter. Lilac is what was sent, peach is the reply.
        "ask": "#d7b3ff",
        "reply": "#ffb085",
    }
)

# Same hue as theme style "you". Active while the terminal echoes a line.
_YOU_ON = "\033[38;2;142;203;255m"
_YOU_OFF = "\033[0m"
Stream = Literal["you", "log", "out"]
_STREAM_LABEL: dict[Stream, str] = {"you": "in", "log": "log", "out": "out"}

_MARK: dict[Kind, str] = {
    "ok": "✓",
    "bad": "✗",
    "warn": "!",
    "info": "·",
    "pass": "✓",
    "fail": "✗",
    "run": "…",
}

console = Console(theme=_THEME, highlight=False, soft_wrap=True)


def _fold(text: str, width: int) -> list[str]:
    """Wrap on spaces. A token longer than ``width`` breaks so a path cannot run off."""
    rows: list[str] = []
    for raw in text.splitlines() or [""]:
        current = ""
        for word in raw.split(" "):
            while len(word) > width:
                if current:
                    rows.append(current)
                    current = ""
                rows.append(word[:width])
                word = word[width:]
            if not word:
                continue
            candidate = word if not current else f"{current} {word}"
            if current and len(candidate) > width:
                rows.append(current)
                current = word
            else:
                current = candidate
        rows.append(current)
    return rows


def transcript(kind: Stream, message: str, *, style: str | None = None) -> None:
    """One chat row. ``you`` is what was typed, ``log`` is the agent log, ``out`` is the answer.

    ``style`` overrides the hue (an error log stays in the log gutter, painted as a failure).
    Wrapped lines keep the gutter width, and nothing is padded out to the terminal edge.
    """
    if not message:
        return
    paint = style or kind
    label = _STREAM_LABEL[kind]
    # Leave one column free so a full line does not wrap and leave a blank row.
    width = max(24, console.width - 5)
    lines = _fold(message, width)
    gutter = " " * 4
    for index, line in enumerate(lines):
        prefix = f"{label:<3} " if index == 0 else gutter
        console.print(Text(prefix + line, style=paint), soft_wrap=False)
    _bump_line()


def echo_input_color(stream: IO[str], *, on: bool) -> None:
    """Color the terminal's echo of the line being typed. No effect when color is off."""
    if os.environ.get("NO_COLOR") or not console.is_terminal:
        return
    stream.write(_YOU_ON if on else _YOU_OFF)
    stream.flush()


# Counts human lines so a step can rewrite its own "…" only when nothing else
# printed in between (nested pipeline steps stay intact).
_line_seq = 0


def _bump_line() -> int:
    global _line_seq
    _line_seq += 1
    return _line_seq


def say(message: str, *, kind: Kind = "ok") -> None:
    """Print one human line: colored mark + message."""
    mark = _MARK[kind]
    console.print(f"[{kind}]{mark}[/{kind}]  {message}")
    _bump_line()


def line_mark() -> int:
    """Snapshot after a status line; used to decide in-place rewrite."""
    return _line_seq


def replace_status(message: str, *, kind: Kind, mark: int) -> bool:
    """Rewrite the previous status line when nothing else printed since ``mark``."""
    if not (console.is_terminal and not console.is_dumb_terminal and mark == _line_seq):
        return False
    glyph = _MARK[kind]
    console.print(f"\033[1A\r[{kind}]{glyph}[/{kind}]  {message}\033[K")
    _bump_line()
    return True


def fail(code: str, detail: str = "") -> None:
    """Print a CAP error code (and optional detail) for humans."""
    if detail:
        console.print(f"[bad]✗[/bad]  [bad]{code}[/bad]  [mute]{detail}[/mute]")
    else:
        console.print(f"[bad]✗[/bad]  [bad]{code}[/bad]")
    _bump_line()


def format_duration(seconds: float) -> str:
    """Compact wall-clock for step logs (ms under 1s, otherwise seconds)."""
    if seconds < 1:
        return f"{max(seconds, 0.0) * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rem = divmod(seconds, 60)
    return f"{int(minutes)}m{rem:04.1f}s"


def report(
    title: str,
    rows: Sequence[tuple[str, str]],
    *,
    subtitle: str = "",
) -> None:
    """Print a labeled two-column panel (human mode only)."""
    table = Table.grid(padding=(0, 2), pad_edge=False)
    table.add_column(style="label", justify="right", min_width=12)
    table.add_column()
    for label, value in rows:
        table.add_row(label, value)
    console.print(
        Panel(
            table,
            title=f"[hero]{title}[/hero]",
            subtitle=f"[mute]{subtitle}[/mute]" if subtitle else None,
            border_style="rule",
            padding=(1, 2),
        )
    )
    _bump_line()


def celebrate_login(team_id: str) -> None:
    """Short confirmation after a successful ``city login``."""
    console.print()
    console.print(f"[ok]✓[/ok]  Signed in as [city]{team_id}[/city]")
    console.print("[mute]Ready to init, validate, and deploy agents.[/mute]")
    console.print()
    _bump_line()


def brand_line() -> str:
    """Short tagline for help / epilog."""
    return "city — deploy agents to Milano Agentic City"
