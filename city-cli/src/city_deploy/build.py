"""Agent image: city-sdk base layers plus one source layer, pushed and pinned by digest."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from cap.core.errors import CapError

from city_deploy.image import Recipe, SourceFile, build_layer, image_config, image_manifest
from city_deploy.registry import ImageRef, Registry

_SEGMENT = re.compile(r"^[a-z0-9]+(?:(?:_|__|-+)[a-z0-9]+)*$")


@dataclass(frozen=True)
class BuiltImage:
    ref: str
    base: str
    layer: str
    seconds: float


class ImageBuilder(Protocol):
    def build(
        self,
        *,
        agent_id: str,
        version: str,
        recipe: Recipe,
        files: Mapping[str, SourceFile],
    ) -> BuiltImage: ...


@dataclass(frozen=True)
class _Base:
    ref: str
    name: str
    manifest: dict[str, Any]
    config: dict[str, Any]

    @property
    def workdir(self) -> str:
        inner = self.config.get("config") or {}
        return str(inner.get("WorkingDir") or "/")

    @property
    def layers(self) -> list[dict[str, Any]]:
        return list(self.manifest.get("layers", []))


class RegistryBuilder:
    """Pushes to ``<target>/<team>/<agent>``. Base metadata and blob presence are cached."""

    def __init__(self, registry: Registry, *, base_image: str, target: str) -> None:
        base = ImageRef.parse(base_image)
        if base.digest is None:
            raise CapError("POLICY_VIOLATION", "CITY_SDK_IMAGE must be pinned by digest")
        host, _, prefix = target.strip().rstrip("/").partition("/")
        if host != registry.host or base.host != registry.host or prefix.count("/") != 1:
            raise CapError("POLICY_VIOLATION", "CITY_AGENT_REGISTRY")
        self.registry = registry
        self.base_image = base_image.strip()
        self.prefix = prefix
        self._base_ref = base
        self._base: _Base | None = None
        self._present: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    def warm(self) -> None:
        """Load the base and seed its blobs into the agent repository before the first deploy."""

        base = self._load_base()
        self._ensure_base(f"{self.prefix}/city-sdk", base)

    def build(
        self,
        *,
        agent_id: str,
        version: str,
        recipe: Recipe,
        files: Mapping[str, SourceFile],
    ) -> BuiltImage:
        started = time.perf_counter()
        name = self._image_name(agent_id)
        base = self._load_base()
        layer = build_layer(recipe, files, workdir=base.workdir)
        labels = {
            "city.milano/agent": agent_id,
            "city.milano/version": version,
            "org.opencontainers.image.base.name": self.base_image.split("@", 1)[0],
            "org.opencontainers.image.base.digest": str(self._base_ref.digest),
        }
        config = image_config(base.config, recipe, layer, labels=labels)
        manifest, media = image_manifest(base.manifest, config, layer)
        self._ensure_base(name, base)
        self.registry.push_blob(name, layer.blob, layer.digest)
        self.registry.push_blob(name, config, "sha256:" + hashlib.sha256(config).hexdigest())
        digest = self.registry.put_manifest(name, version, manifest, media)
        return BuiltImage(
            ref=f"{self.registry.host}/{name}@{digest}",
            base=self.base_image,
            layer=layer.digest,
            seconds=time.perf_counter() - started,
        )

    def _image_name(self, agent_id: str) -> str:
        parts = agent_id.removeprefix("agent:").split("/")
        if len(parts) > 2 or any(_SEGMENT.fullmatch(part) is None for part in parts):
            raise CapError("SCHEMA_INVALID", "agent_id")
        return "/".join((self.prefix, *parts))

    def _load_base(self) -> _Base:
        with self._lock:
            if self._base is not None:
                return self._base
        ref = self._base_ref
        body, _media = self.registry.manifest(ref.name, ref.reference)
        manifest = json.loads(body)
        config = json.loads(self.registry.blob(ref.name, str(manifest["config"]["digest"])))
        loaded = _Base(ref=self.base_image, name=ref.name, manifest=manifest, config=config)
        with self._lock:
            self._base = loaded
        return loaded

    def _ensure_base(self, name: str, base: _Base) -> None:
        for row in base.layers:
            digest = str(row["digest"])
            key = (name, digest)
            with self._lock:
                if key in self._present:
                    continue
            if not self.registry.has_blob(name, digest):
                self.registry.mount_or_copy(name, digest, source=base.name, size=int(row["size"]))
            with self._lock:
                self._present.add(key)
