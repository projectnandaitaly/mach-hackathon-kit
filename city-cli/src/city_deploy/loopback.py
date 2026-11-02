"""Loopback redirect receiver for ``city login`` (like ``gcloud auth login``)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from cap.core.errors import CapError

_DONE_PAGE = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>city login</title></head>
<body style="font-family:system-ui;background:#0b0f14;color:#e6edf3;text-align:center;padding:4rem">
<h1>You're signed in to Milano Agentic City</h1>
<p>Your terminal has the session. You can close this tab.</p>
</body></html>"""

_FAIL_PAGE = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>city login</title></head>
<body style="font-family:system-ui;background:#0b0f14;color:#e6edf3;text-align:center;padding:4rem">
<h1>Login was not completed</h1>
<p>Return to your terminal and run <code>city login</code> again.</p>
</body></html>"""


@dataclass(frozen=True)
class Pkce:
    verifier: str
    challenge: str


def new_pkce() -> Pkce:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return Pkce(verifier, challenge)


@dataclass
class _Outcome:
    event: threading.Event = field(default_factory=threading.Event)
    code: str | None = None
    error: str | None = None


class LoopbackReceiver:
    """One-shot HTTP listener on ``127.0.0.1`` that accepts a single callback."""

    def __init__(self) -> None:
        self.state = secrets.token_urlsafe(16)
        self._outcome = _Outcome()
        self._server = HTTPServer(("127.0.0.1", 0), _handler(self.state, self._outcome))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def redirect_uri(self) -> str:
        port = self._server.server_address[1]
        return f"http://127.0.0.1:{port}/callback"

    def __enter__(self) -> LoopbackReceiver:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def wait(self, timeout_s: float) -> str:
        if not self._outcome.event.wait(timeout_s):
            raise CapError("POLICY_VIOLATION", "authorization timed out")
        if self._outcome.code is None:
            raise CapError("POLICY_VIOLATION", self._outcome.error or "login not completed")
        return self._outcome.code


def _handler(state: str, outcome: _Outcome) -> type[BaseHTTPRequestHandler]:
    class _Callback(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path != "/callback":
                self._reply(404, b"not found", "text/plain")
                return
            query = parse_qs(url.query)
            got_state = (query.get("state") or [""])[0]
            # Ignore stray requests that do not carry our state; keep listening.
            if not hmac.compare_digest(got_state.encode(), state.encode()):
                self._reply(400, _FAIL_PAGE, "text/html; charset=utf-8")
                return
            code = (query.get("code") or [""])[0]
            if outcome.event.is_set():
                self._reply(409, _FAIL_PAGE, "text/html; charset=utf-8")
                return
            if code:
                outcome.code = code
                self._reply(200, _DONE_PAGE, "text/html; charset=utf-8")
            else:
                outcome.error = (query.get("error") or ["login not completed"])[0][:120]
                self._reply(400, _FAIL_PAGE, "text/html; charset=utf-8")
            outcome.event.set()

        def _reply(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return _Callback
