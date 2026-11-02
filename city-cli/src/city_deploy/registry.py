"""OCI distribution client for Artifact Registry. Tokens never reach logs, errors, or URLs."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass

import httpx
from cap.core.errors import CapError

DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
_INDEXES = frozenset(
    {
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.index.v1+json",
    }
)
_ACCEPT = ", ".join([DOCKER_MANIFEST, OCI_MANIFEST, *sorted(_INDEXES)])
_REF = re.compile(
    r"^(?P<host>[a-z0-9]+(?:[.-][a-z0-9]+)*-docker\.pkg\.dev)"
    r"/(?P<name>[a-z0-9][a-z0-9._/-]{0,200}?)"
    r"(?:@(?P<digest>sha256:[0-9a-f]{64})|:(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}))$"
)
_CHALLENGE = re.compile(r'(\w+)="([^"]*)"')


class RegistryError(Exception):
    """A registry call failed. The message carries the method, path, and status only."""


@dataclass(frozen=True)
class ImageRef:
    host: str
    name: str
    digest: str | None = None
    tag: str | None = None

    @classmethod
    def parse(cls, text: str) -> ImageRef:
        found = _REF.fullmatch(text.strip())
        if found is None or ".." in text or "//" in text:
            raise CapError("SCHEMA_INVALID", "image reference")
        return cls(
            host=found.group("host"),
            name=found.group("name"),
            digest=found.group("digest"),
            tag=found.group("tag"),
        )

    @property
    def reference(self) -> str:
        return self.digest or self.tag or "latest"


class Registry:
    """One kept-alive client per registry host. Safe to share across threads."""

    def __init__(
        self,
        host: str,
        *,
        token: Callable[[], str],
        transport: httpx.BaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.host = host
        self._google = token
        self._client = httpx.Client(
            base_url=f"https://{host}",
            transport=transport,
            timeout=httpx.Timeout(timeout, connect=10.0),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=16),
        )
        self._tokens: dict[str, str] = {}
        self._lock = threading.Lock()

    def close(self) -> None:
        self._client.close()

    # --- reads --------------------------------------------------------------------

    def manifest(self, name: str, reference: str) -> tuple[bytes, str]:
        """Single-platform manifest bytes and media type. An index resolves to linux/amd64."""

        body, media = self._manifest(name, reference)
        if media not in _INDEXES:
            return body, media
        index = json.loads(body)
        for row in index.get("manifests", []):
            platform = row.get("platform") or {}
            if platform.get("os") == "linux" and platform.get("architecture") == "amd64":
                return self._manifest(name, str(row["digest"]))
        raise RegistryError(f"GET /v2/{name}/manifests: no linux/amd64 image")

    def _manifest(self, name: str, reference: str) -> tuple[bytes, str]:
        response = self._send(
            "GET",
            f"/v2/{name}/manifests/{reference}",
            scope=_pull(name),
            headers={"Accept": _ACCEPT},
        )
        _expect(response, {200})
        media = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
        return response.content, media

    def blob(self, name: str, digest: str) -> bytes:
        with self._open_blob(name, digest) as response:
            data = response.read()
        if _sha256(data) != digest:
            raise RegistryError(f"GET /v2/{name}/blobs: digest mismatch")
        return data

    def has_blob(self, name: str, digest: str) -> bool:
        response = self._send("HEAD", f"/v2/{name}/blobs/{digest}", scope=_pull(name))
        if response.status_code == 404:
            return False
        _expect(response, {200, 307})
        return True

    # --- writes -------------------------------------------------------------------

    def push_blob(self, name: str, data: bytes, digest: str) -> None:
        location = self._start_upload(name, params=None)
        self._finish_upload(name, location, digest, content=data, size=len(data), scope=_push(name))

    def mount_or_copy(self, name: str, digest: str, *, source: str, size: int) -> None:
        """Make ``digest`` present in ``name``: mount from ``source``, else stream a copy."""

        scope = f"{_push(name)} {_pull(source)}"
        response = self._send(
            "POST",
            f"/v2/{name}/blobs/uploads/",
            scope=scope,
            params={"mount": digest, "from": source},
            headers={"Content-Length": "0"},
        )
        if response.status_code == 201:
            return
        _expect(response, {202})
        location = _location(response)
        with self._open_blob(source, digest) as stream:
            body = iter([stream.content]) if stream.is_stream_consumed else stream.iter_raw()
            self._finish_upload(name, location, digest, content=body, size=size, scope=scope)

    def put_manifest(self, name: str, reference: str, body: bytes, media_type: str) -> str:
        response = self._send(
            "PUT",
            f"/v2/{name}/manifests/{reference}",
            scope=_push(name),
            headers={"Content-Type": media_type},
            content=body,
        )
        _expect(response, {201, 200})
        digest = _sha256(body)
        served = response.headers.get("Docker-Content-Digest")
        if served and served != digest:
            raise RegistryError(f"PUT /v2/{name}/manifests: digest mismatch")
        return digest

    def _start_upload(self, name: str, *, params: Mapping[str, str] | None) -> str:
        response = self._send(
            "POST",
            f"/v2/{name}/blobs/uploads/",
            scope=_push(name),
            params=params,
            headers={"Content-Length": "0"},
        )
        _expect(response, {202})
        return _location(response)

    def _finish_upload(
        self,
        name: str,
        location: str,
        digest: str,
        *,
        content: bytes | Iterator[bytes],
        size: int,
        scope: str,
    ) -> None:
        url = httpx.URL(location)
        if not url.is_absolute_url:
            url = self._client.base_url.join(location)
        url = url.copy_merge_params({"digest": digest})
        if url.host != self.host:
            raise RegistryError(f"PUT /v2/{name}/blobs/uploads: unexpected host")
        response = self._send(
            "PUT",
            str(url),
            scope=scope,
            headers={"Content-Type": "application/octet-stream", "Content-Length": str(size)},
            content=content,
        )
        _expect(response, {201, 204})

    @contextmanager
    def _open_blob(self, name: str, digest: str) -> Iterator[httpx.Response]:
        path = f"/v2/{name}/blobs/{digest}"
        response = self._send("GET", path, scope=_pull(name), stream=True)
        try:
            if response.status_code in {301, 302, 303, 307, 308}:
                target = response.headers.get("Location", "")
                response.close()
                # The redirect target is a signed storage URL; it gets no registry credentials.
                response = self._client.send(self._client.build_request("GET", target), stream=True)
            _expect(response, {200})
            yield response
        finally:
            response.close()

    # --- auth ---------------------------------------------------------------------

    def _send(
        self,
        method: str,
        url: str,
        *,
        scope: str,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        content: bytes | Iterator[bytes] | None = None,
        stream: bool = False,
    ) -> httpx.Response:
        response = self._once(method, url, scope, params, headers, content, stream)
        if response.status_code != 401 or isinstance(content, Iterator):
            return response
        challenge = response.headers.get("WWW-Authenticate", "")
        response.close()
        with self._lock:
            self._tokens.pop(scope, None)
        if challenge.lower().startswith("bearer "):
            self._exchange(challenge, scope)
        return self._once(method, url, scope, params, headers, content, stream)

    def _once(
        self,
        method: str,
        url: str,
        scope: str,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
        content: bytes | Iterator[bytes] | None,
        stream: bool,
    ) -> httpx.Response:
        outbound = dict(headers or {})
        with self._lock:
            token = self._tokens.get(scope)
        outbound["Authorization"] = f"Bearer {token or self._google()}"
        request = self._client.build_request(
            method, url, params=params, headers=outbound, content=content
        )
        try:
            return self._client.send(request, stream=stream)
        except httpx.HTTPError as exc:
            raise RegistryError(f"{method} {request.url.path} failed") from exc

    def _exchange(self, challenge: str, scope: str) -> None:
        fields = dict(_CHALLENGE.findall(challenge))
        realm = fields.get("realm", "")
        realm_url = httpx.URL(realm)
        if realm_url.scheme != "https" or realm_url.host != self.host:
            raise RegistryError("registry token realm is not the registry host")
        params = httpx.QueryParams(
            [("service", fields.get("service", self.host))]
            + [("scope", part) for part in scope.split()]
        )
        try:
            response = self._client.get(
                realm,
                params=params,
                auth=httpx.BasicAuth("oauth2accesstoken", self._google()),
            )
        except httpx.HTTPError as exc:
            raise RegistryError("registry token exchange failed") from exc
        _expect(response, {200})
        body = response.json()
        token = body.get("token") or body.get("access_token")
        if not isinstance(token, str) or not token:
            raise RegistryError("registry token exchange returned no token")
        with self._lock:
            self._tokens[scope] = token


def _pull(name: str) -> str:
    return f"repository:{name}:pull"


def _push(name: str) -> str:
    return f"repository:{name}:pull,push"


def _location(response: httpx.Response) -> str:
    location = str(response.headers.get("Location", ""))
    if not location:
        raise RegistryError("blob upload returned no location")
    return location


def _expect(response: httpx.Response, ok: set[int]) -> None:
    if response.status_code in ok:
        return
    path = response.request.url.path
    raise RegistryError(f"{response.request.method} {path} returned HTTP {response.status_code}")


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()
