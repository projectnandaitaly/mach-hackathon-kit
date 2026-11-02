"""Load the City LLM Gateway credential from ``city login`` (real endpoint)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from cap.core.errors import CapError

DEFAULT_GATEWAY_URL = "https://llm.projectnandaitaly.ai"


def _config_home() -> Path:
    override = os.environ.get("CITY_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg).expanduser().resolve() / "city"
    return Path.home() / ".config" / "city"


def _credentials_mode() -> str:
    return os.environ.get("CITY_CREDENTIALS", "").strip().lower()


def _llm_vk_path(team_id: str, *, root: Path | None = None) -> Path:
    if _credentials_mode() == "project":
        base = (root or Path(".")).resolve() / ".city"
        return base / "llm.vk"
    return _config_home() / "teams" / team_id / "llm.vk"


def _read_vk(path: Path) -> str | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def _login_llm_key(*, root: Path | None = None) -> str | None:
    """VK downloaded by ``city login`` — not invented locally."""
    if _credentials_mode() == "project":
        return _read_vk((root or Path(".")).resolve() / ".city" / "llm.vk")
    teams = _config_home() / "teams"
    if not teams.is_dir():
        return None
    candidates: list[Path] = []
    for child in teams.iterdir():
        if not child.is_dir():
            continue
        path = child / "llm.vk"
        if path.is_file():
            candidates.append(path)
    if not candidates:
        return None
    newest = max(candidates, key=lambda path: path.stat().st_mtime)
    return _read_vk(newest)


def load_team_gateway(
    *,
    team_id: str | None = None,
    root: Path | None = None,
) -> tuple[str | None, str]:
    """Return ``(virtual_key, gateway_url)`` for the live City LLM Gateway.

    Preference: ``CITY_LLM_VIRTUAL_KEY``, then the named team's ``llm.vk``,
    then the newest ``llm.vk`` written by ``city login``.
    """
    url = os.environ.get("CITY_LLM_GATEWAY_URL", "").strip() or DEFAULT_GATEWAY_URL
    key = os.environ.get("CITY_LLM_VIRTUAL_KEY", "").strip()
    if key:
        return key, url
    slug = (team_id or os.environ.get("CITY_TEAM", "")).strip()
    if slug:
        keyed = _read_vk(_llm_vk_path(slug, root=root))
        if keyed:
            return keyed, url
    return _login_llm_key(root=root), url


def load_gateway(
    env: Mapping[str, str],
    *,
    team_id: str | None = None,
    root: Path | None = None,
) -> tuple[str | None, str | None]:
    """Return ``(virtual_key, gateway_url)`` for this process, in a pod or on a laptop.

    In a release (``CITY_ENV=cloud``) the key is the file Deploy mounts at ``CITY_LLM_KEY_PATH``
    and the URL is ``CITY_LLM_URL``; laptop variables are ignored. Elsewhere it is
    ``load_team_gateway``: the key ``city login`` wrote for the team.
    """
    if env.get("CITY_ENV") != "cloud":
        return load_team_gateway(team_id=team_id or env.get("CITY_TEAM"), root=root)
    raw = env.get("CITY_LLM_URL", "").strip()
    if not raw:
        return None, None
    parsed = urlsplit(raw)
    if parsed.scheme != "https" or not parsed.hostname:
        raise CapError("POLICY_VIOLATION", "CITY_LLM_URL")
    url = raw.rstrip("/")
    key_path = env.get("CITY_LLM_KEY_PATH", "").strip()
    return (_read_vk(Path(key_path)) if key_path else None), url


def inject_team_gateway(*, team_id: str | None = None, root: Path | None = None) -> str:
    """Set ``CITY_LLM_*`` from ``city login`` when missing. Returns status."""
    key, url = load_team_gateway(team_id=team_id, root=root)
    os.environ.setdefault("CITY_LLM_GATEWAY_URL", url)
    if key:
        os.environ["CITY_LLM_VIRTUAL_KEY"] = key
        return "loaded"
    return "missing"
