"""Team-scoped credential store (XDG config home).

Default layout::

    $XDG_CONFIG_HOME/city/
      active                 # team_id from the latest ``city login``
      teams/<team_id>/
        session              # deploy API bearer (not the controller private key)
        controller.pem       # Team Controller Key (0600)
        controller.jwk       # public half (convenience)
        llm.vk               # City LLM Gateway virtual key (0600)

``CITY_HOME`` overrides the config root (tests / CI).
``CITY_CREDENTIALS=project`` keeps credentials under ``<root>/.city/`` instead
(explicit project-local override; not the default).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from cap.core.errors import CapError

_TEAM = re.compile(r"^[a-z0-9_-]+$")


def config_home() -> Path:
    """Root directory that contains ``teams/<team_id>/``."""
    override = os.environ.get("CITY_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg).expanduser().resolve() / "city"
    return Path.home() / ".config" / "city"


def credentials_mode() -> str:
    raw = os.environ.get("CITY_CREDENTIALS", "").strip().lower()
    if raw in {"", "user", "xdg"}:
        return "user"
    if raw == "project":
        return "project"
    raise CapError("SCHEMA_INVALID", "CITY_CREDENTIALS")


def _active_path(*, root: Path | None = None) -> Path:
    if credentials_mode() == "project":
        return (root or Path(".")).resolve() / ".city" / "active"
    return config_home() / "active"


def write_active_team(team_id: str, *, root: Path | None = None) -> Path:
    """Record ``team_id`` as the current login (used by ``city init agent``)."""
    if _TEAM.fullmatch(team_id) is None:
        raise CapError("SCHEMA_INVALID", "team")
    path = _active_path(root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    path.write_text(team_id + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def resolve_active_team(*, root: Path | None = None) -> str:
    """Return the logged-in team id, or raise when ``city login`` is missing."""
    pointed = _read_active(root=root)
    if pointed is not None:
        store = TeamStore.open(pointed, root=root)
        if store.read_session():
            return pointed
    newest = _newest_session_team(root=root)
    if newest is not None:
        return newest
    raise CapError("POLICY_VIOLATION", "city login required")


def _read_active(*, root: Path | None = None) -> str | None:
    path = _active_path(root=root)
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    if not text or _TEAM.fullmatch(text) is None:
        return None
    return text


def _newest_session_team(*, root: Path | None = None) -> str | None:
    if credentials_mode() == "project":
        store_root = (root or Path(".")).resolve() / ".city"
        if not (store_root / "session").is_file():
            return None
        # Project mode has no team folder; only ``active`` names the team.
        return _read_active(root=root)
    teams = config_home() / "teams"
    if not teams.is_dir():
        return None
    candidates: list[tuple[float, str]] = []
    for child in teams.iterdir():
        if not child.is_dir() or _TEAM.fullmatch(child.name) is None:
            continue
        session = child / "session"
        if not session.is_file():
            continue
        if not session.read_text(encoding="utf-8").strip():
            continue
        candidates.append((session.stat().st_mtime, child.name))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


class TeamStore:
    """Files for one team's session + controller key."""

    def __init__(self, team_id: str, path: Path) -> None:
        if _TEAM.fullmatch(team_id) is None:
            raise CapError("SCHEMA_INVALID", "team")
        self.team_id = team_id
        self.path = path

    @classmethod
    def open(cls, team_id: str, *, root: Path | None = None) -> TeamStore:
        """Open the store for ``team_id``.

        With ``CITY_CREDENTIALS=project``, ``root`` (default ``.``) selects
        ``<root>/.city``. Otherwise uses ``$CITY_HOME`` / XDG config.
        """
        if _TEAM.fullmatch(team_id) is None:
            raise CapError("SCHEMA_INVALID", "team")
        if credentials_mode() == "project":
            base = (root or Path(".")).resolve() / ".city"
        else:
            base = config_home() / "teams" / team_id
        return cls(team_id, base)

    def ensure_dir(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        try:
            self.path.chmod(0o700)
        except OSError:
            pass

    @property
    def session_path(self) -> Path:
        return self.path / "session"

    @property
    def controller_pem_path(self) -> Path:
        return self.path / "controller.pem"

    @property
    def controller_jwk_path(self) -> Path:
        return self.path / "controller.jwk"

    @property
    def llm_vk_path(self) -> Path:
        return self.path / "llm.vk"

    def write_session(self, token: str) -> Path:
        self.ensure_dir()
        path = self.session_path
        path.write_text(token, encoding="utf-8")
        path.chmod(0o600)
        return path

    def read_session(self) -> str | None:
        path = self.session_path
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8").strip()
        return text or None

    def write_llm_key(self, virtual_key: str) -> Path:
        self.ensure_dir()
        path = self.llm_vk_path
        path.write_text(virtual_key.strip(), encoding="utf-8")
        path.chmod(0o600)
        return path

    def read_llm_key(self) -> str | None:
        path = self.llm_vk_path
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8").strip()
        return text or None
