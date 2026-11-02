"""Team Controller Key helpers for the city CLI."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
from cap.core.errors import CapError
from cap.core.keys import PrivateKey
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from city_deploy.store import TeamStore


def load_controller(store: TeamStore) -> PrivateKey:
    try:
        data = store.controller_pem_path.read_bytes()
    except OSError as exc:
        raise CapError("KEY_UNKNOWN", "controller") from exc
    loaded = serialization.load_pem_private_key(data, password=None)
    if not isinstance(loaded, Ed25519PrivateKey):
        raise CapError("KEY_UNKNOWN", "controller")
    return PrivateKey(loaded)


def ensure_controller_key(
    store: TeamStore,
    register: Callable[[dict[str, Any]], httpx.Response],
    *,
    overwrite: bool,
) -> dict[str, Any]:
    """Ensure a controller key exists in ``store`` and register its public JWK.

    When ``overwrite`` is false and a PEM already exists, reuse it (login path).
    When ``overwrite`` is true, always generate a new key (``city keys init``)
    and retire only that previous public key. Another teammate's key is untouched.

    ``register`` receives the ``/keys/register`` body.
    """
    store.ensure_dir()
    key_path = store.controller_pem_path
    jwk_path = store.controller_jwk_path
    if key_path.is_file() and not overwrite:
        public = load_controller(store).public_key().to_jwk()
        jwk_path.write_text(json.dumps(public), encoding="utf-8")
        _post_key(register, public, None)
        return public
    previous = load_controller(store).public_key().to_jwk() if key_path.is_file() else None
    raw = Ed25519PrivateKey.generate()
    private = PrivateKey(raw)
    public = private.public_key().to_jwk()
    retire = previous if previous is not None and previous.get("x") != public.get("x") else None
    _post_key(register, public, retire)
    pem = raw.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path.write_bytes(pem)
    key_path.chmod(0o600)
    jwk_path.write_text(json.dumps(public), encoding="utf-8")
    return public


def _post_key(
    register: Callable[[dict[str, Any]], httpx.Response],
    public: dict[str, Any],
    retire: dict[str, Any] | None,
) -> None:
    body: dict[str, Any] = {"jwk": public}
    if retire is not None:
        body["retire"] = retire
    response = register(body)
    if response.status_code >= 400:
        raise _register_error(response)


def _register_error(response: httpx.Response) -> CapError:
    try:
        payload = response.json()
    except ValueError:
        return CapError("KEY_UNKNOWN", "register")
    if not isinstance(payload, dict):
        return CapError("KEY_UNKNOWN", "register")
    code = payload.get("code")
    if not isinstance(code, str) or not code:
        return CapError("KEY_UNKNOWN", "register")
    message = payload.get("message")
    detail = message if isinstance(message, str) else "register"
    return CapError(code, detail)
