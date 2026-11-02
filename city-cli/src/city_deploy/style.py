"""Human CLI chrome. JSON mode stays plain; respects NO_COLOR / non-TTY."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
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
    }
)

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
