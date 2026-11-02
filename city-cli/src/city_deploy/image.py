"""Agent image assembly on the city-sdk base. No step runs team code."""

from __future__ import annotations

import base64
import binascii
import fnmatch
import gzip
import hashlib
import io
import json
import os
import posixpath
import re
import shlex
import tarfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cap.core.errors import CapError

MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_SOURCE_FILES = 2000
_BASE = re.compile(r"^city-sdk(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?$")
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_VAR = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
_GLOB = frozenset("*?[")
# Never part of an image: the team keystore, VCS, virtualenvs, caches, and dotenv files.
_SKIP_DIRS = frozenset(
    {".city", ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)
_REFUSED_DIRS = frozenset({".city"})
_EPOCH = "1970-01-01T00:00:00Z"


@dataclass(frozen=True)
class SourceFile:
    data: bytes
    executable: bool = False


@dataclass(frozen=True)
class Op:
    kind: str
    args: tuple[str, ...]


@dataclass
class Recipe:
    ops: list[Op] = field(default_factory=list)
    env: list[tuple[str, str]] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    exposed: list[str] = field(default_factory=list)
    user: str | None = None
    entrypoint: list[str] | None = None
    cmd: list[str] | None = None
    clear_cmd: bool = False

    def sources(self) -> tuple[str, ...]:
        found: list[str] = []
        for op in self.ops:
            if op.kind == "COPY":
                found.extend(op.args[:-1])
        return tuple(found)


@dataclass(frozen=True)
class Layer:
    blob: bytes
    digest: str
    diff_id: str

    @property
    def size(self) -> int:
        return len(self.blob)


def parse_dockerfile(text: str) -> Recipe:
    rows = _instructions(text)
    if not rows:
        raise _refuse("Dockerfile must start FROM city-sdk")
    head, base = rows[0]
    if head != "FROM" or _BASE.fullmatch(base.strip()) is None:
        raise _refuse("Dockerfile must start FROM city-sdk (single stage)")
    recipe = Recipe()
    for word, rest in rows[1:]:
        _apply(recipe, word, rest)
    return recipe


def _apply(recipe: Recipe, word: str, rest: str) -> None:
    if word == "WORKDIR":
        recipe.ops.append(Op("WORKDIR", (_path(rest.strip(), "WORKDIR"),)))
    elif word == "COPY":
        recipe.ops.append(Op("COPY", _copy_args(rest)))
    elif word == "ENV":
        recipe.env.extend(_pairs(rest, legacy=True))
    elif word == "LABEL":
        recipe.labels.update(_pairs(rest, legacy=False))
    elif word == "USER":
        user = rest.strip()
        if not user or user.split(":", 1)[0] in {"root", "0"} or "$" in user:
            raise _refuse("USER root is not allowed; the base user is city")
        recipe.user = user
    elif word == "ENTRYPOINT":
        recipe.entrypoint = _command(rest)
        recipe.cmd = None
        recipe.clear_cmd = True
    elif word == "CMD":
        recipe.cmd = _command(rest)
        recipe.clear_cmd = False
    elif word == "EXPOSE":
        for port in rest.split():
            if re.fullmatch(r"[0-9]{1,5}(?:/(?:tcp|udp))?", port) is None:
                raise _refuse(f"EXPOSE {port}")
            recipe.exposed.append(port if "/" in port else port + "/tcp")
    elif word == "FROM":
        raise _refuse("FROM appears twice; multi-stage builds are not supported")
    else:
        raise _refuse(
            f"Dockerfile {word} is not supported; the city assembles images from "
            "FROM city-sdk with COPY, ENV, WORKDIR, USER, ENTRYPOINT, CMD, LABEL, EXPOSE"
        )


def _instructions(text: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    pending = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not pending and (not stripped or stripped.startswith("#")):
            continue
        if pending and stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        line = (pending + stripped).strip()
        pending = ""
        word, _, rest = line.partition(" ")
        rows.append((word.upper(), rest.strip()))
    if pending.strip():
        word, _, rest = pending.strip().partition(" ")
        rows.append((word.upper(), rest.strip()))
    return rows


def _copy_args(rest: str) -> tuple[str, ...]:
    if rest.startswith("["):
        args = _json_list(rest, "COPY")
    else:
        args = rest.split()
    flags = [arg for arg in args if arg.startswith("--")]
    if flags:
        name = flags[0].split("=", 1)[0]
        raise _refuse(f"COPY {name} is not supported")
    if len(args) < 2:
        raise _refuse("COPY needs a source and a destination")
    sources = tuple(_source(arg) for arg in args[:-1])
    dest = args[-1]
    if "$" in dest:
        raise _refuse("COPY variable expansion is not supported")
    return (*sources, dest)


def _source(value: str) -> str:
    if "$" in value:
        raise _refuse("COPY variable expansion is not supported")
    if value.startswith("/"):
        raise _refuse(f"COPY source {value} is outside the agent directory")
    normal = posixpath.normpath(value)
    if normal == ".." or normal.startswith("../"):
        raise _refuse(f"COPY source {value} is outside the agent directory")
    if set(normal.split("/")) & _REFUSED_DIRS:
        raise _refuse(f"COPY source {value} is the team keystore")
    return normal


def _path(value: str, word: str) -> str:
    if not value or "$" in value:
        raise _refuse(f"{word} variable expansion is not supported")
    return value


def _command(rest: str) -> list[str]:
    if rest.startswith("["):
        return _json_list(rest, "ENTRYPOINT/CMD")
    return ["/bin/sh", "-c", rest]


def _json_list(rest: str, word: str) -> list[str]:
    try:
        parsed = json.loads(rest)
    except ValueError as exc:
        raise _refuse(f"{word} JSON form is invalid") from exc
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise _refuse(f"{word} JSON form must be a list of strings")
    return list(parsed)


def _pairs(rest: str, *, legacy: bool) -> list[tuple[str, str]]:
    try:
        parts = shlex.split(rest, posix=True)
    except ValueError as exc:
        raise _refuse("ENV/LABEL quoting is invalid") from exc
    if not parts:
        raise _refuse("ENV/LABEL needs a value")
    if legacy and "=" not in parts[0]:
        key, _, value = rest.partition(" ")
        return [(_key(key), value.strip())]
    found: list[tuple[str, str]] = []
    for part in parts:
        key, sep, value = part.partition("=")
        if not sep:
            raise _refuse(f"ENV/LABEL {part} needs key=value")
        found.append((_key(key) if legacy else key, value))
    return found


def _key(value: str) -> str:
    if _KEY.fullmatch(value) is None:
        raise _refuse(f"ENV name {value} is invalid")
    return value


def _refuse(message: str) -> CapError:
    return CapError("POLICY_VIOLATION", message)


# --- Source context ---------------------------------------------------------------


def collect_context(root: Path, recipe: Recipe) -> dict[str, SourceFile]:
    """Files under ``root`` that the recipe copies. Keystore, caches, and dotenv never leave."""

    available: dict[str, Path] = {}
    for current, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name not in _SKIP_DIRS)
        for name in names:
            path = Path(current) / name
            if path.is_symlink() or not path.is_file() or _skip_file(name):
                continue
            available[path.relative_to(root).as_posix()] = path
    wanted = _select(recipe.sources(), available)
    found: dict[str, SourceFile] = {}
    total = 0
    for rel in sorted(wanted):
        path = available[rel]
        data = path.read_bytes()
        total += len(data)
        if total > MAX_SOURCE_BYTES or len(found) >= MAX_SOURCE_FILES:
            raise _refuse("agent source is larger than 8 MiB or 2000 files")
        found[rel] = SourceFile(data, executable=bool(path.stat().st_mode & 0o111))
    return found


def _skip_file(name: str) -> bool:
    return (
        name.endswith((".pyc", ".pyo"))
        or name == ".env"
        or name.startswith(".env.")
        or (name == ".DS_Store")
    )


def _select(sources: Iterable[str], available: Iterable[str]) -> set[str]:
    paths = set(available)
    wanted: set[str] = set()
    for source in sources:
        for context_path, _rel in _expand(source, paths, strict=False):
            wanted.add(context_path)
    return wanted


def encode_files(files: Mapping[str, SourceFile]) -> list[dict[str, object]]:
    return [
        {
            "path": path,
            "content": base64.b64encode(item.data).decode("ascii"),
            "executable": item.executable,
        }
        for path, item in sorted(files.items())
    ]


def decode_files(raw: object) -> dict[str, SourceFile]:
    if not isinstance(raw, list):
        raise CapError("SCHEMA_INVALID", "files")
    if len(raw) > MAX_SOURCE_FILES:
        raise _refuse("agent source is larger than 8 MiB or 2000 files")
    found: dict[str, SourceFile] = {}
    total = 0
    for item in raw:
        if not isinstance(item, dict):
            raise CapError("SCHEMA_INVALID", "files")
        path = _upload_path(item.get("path"))
        content = item.get("content")
        executable = item.get("executable", False)
        if not isinstance(content, str) or not isinstance(executable, bool):
            raise CapError("SCHEMA_INVALID", "files")
        try:
            data = base64.b64decode(content.encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError) as exc:
            raise CapError("SCHEMA_INVALID", "files content") from exc
        total += len(data)
        if total > MAX_SOURCE_BYTES:
            raise _refuse("agent source is larger than 8 MiB or 2000 files")
        if path in found:
            raise CapError("SCHEMA_INVALID", "files duplicate path")
        found[path] = SourceFile(data, executable=executable)
    return found


def _upload_path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise CapError("SCHEMA_INVALID", "files path")
    if "\\" in value or "\x00" in value or value.startswith("/"):
        raise CapError("SCHEMA_INVALID", "files path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise CapError("SCHEMA_INVALID", "files path")
    if set(parts) & _REFUSED_DIRS:
        raise _refuse("the team keystore is never uploaded")
    return value


# --- Layer ------------------------------------------------------------------------


def build_layer(recipe: Recipe, files: Mapping[str, SourceFile], *, workdir: str) -> Layer:
    """One gzip tar layer: root-owned, mtime 0, sorted, so the same source gives the same digest."""

    cwd = _absolute("/", workdir or "/")
    targets: dict[str, SourceFile] = {}
    dirs: set[str] = set()
    known = {cwd}
    paths = set(files)
    for op in recipe.ops:
        if op.kind == "WORKDIR":
            cwd = _absolute(cwd, op.args[0])
            dirs.add(cwd)
            known.add(cwd)
            continue
        sources, dest_raw = op.args[:-1], op.args[-1]
        dest = _absolute(cwd, dest_raw)
        single_file = len(sources) == 1 and sources[0] in paths
        into_dir = (
            dest_raw.endswith("/") or dest_raw in {".", ".."} or dest in known or not single_file
        )
        for source in sources:
            for context_path, rel in _expand(source, paths, strict=True):
                targets[_absolute(dest, rel) if into_dir else dest] = files[context_path]
        if into_dir:
            dirs.add(dest)
            known.add(dest)
    return _pack(targets, dirs)


def _expand(source: str, paths: set[str], *, strict: bool) -> list[tuple[str, str]]:
    """``(context path, path under dest)`` for one COPY source, following Docker rules."""

    if source == ".":
        return [(path, path) for path in sorted(paths)]
    if _GLOB & set(source):
        depth = source.count("/") + 1
        found: list[tuple[str, str]] = []
        entries = {path for path in paths if path.count("/") + 1 >= depth}
        heads = {"/".join(path.split("/")[:depth]) for path in entries}
        for head in sorted(heads):
            if not fnmatch.fnmatchcase(head, source):
                continue
            if head in paths:
                found.append((head, posixpath.basename(head)))
            else:
                found.extend(_directory(head, paths))
        if not found and strict:
            raise CapError("SCHEMA_INVALID", f"COPY source {source} matches nothing")
        return found
    if source in paths:
        return [(source, posixpath.basename(source))]
    inside = _directory(source, paths)
    if not inside and strict:
        raise CapError("SCHEMA_INVALID", f"COPY source {source} was not uploaded")
    return inside


def _directory(prefix: str, paths: set[str]) -> list[tuple[str, str]]:
    lead = prefix.rstrip("/") + "/"
    return [(path, path[len(lead) :]) for path in sorted(paths) if path.startswith(lead)]


def _absolute(cwd: str, value: str) -> str:
    joined = posixpath.normpath(posixpath.join(cwd, value))
    if not joined.startswith("/"):
        joined = "/" + joined
    return joined


def _pack(targets: Mapping[str, SourceFile], dirs: Iterable[str]) -> Layer:
    folders: set[str] = set()
    for start in [*dirs, *(posixpath.dirname(path) for path in targets)]:
        current = start
        while current not in {"", "/"}:
            folders.add(current)
            current = posixpath.dirname(current)
    clash = folders & set(targets)
    if clash:
        raise CapError("SCHEMA_INVALID", f"COPY writes {sorted(clash)[0]} as file and directory")
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name in sorted([*folders, *targets]):
            info = tarfile.TarInfo(name.lstrip("/"))
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            item = targets.get(name)
            if item is None:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
                continue
            info.mode = 0o755 if item.executable else 0o644
            info.size = len(item.data)
            archive.addfile(info, io.BytesIO(item.data))
    tar = raw.getvalue()
    blob = gzip.compress(tar, compresslevel=6, mtime=0)
    return Layer(blob=blob, digest=_sha256(blob), diff_id=_sha256(tar))


# --- Config and manifest ---------------------------------------------------------


def image_config(
    base: Mapping[str, Any],
    recipe: Recipe,
    layer: Layer,
    *,
    labels: Mapping[str, str],
) -> bytes:
    config = json.loads(json.dumps(base))
    inner = config.setdefault("config", {})
    env = _env_map(inner.get("Env") or [])
    for key, value in recipe.env:
        env[key] = _expand_vars(value, env)
    inner["Env"] = [f"{key}={value}" for key, value in env.items()]
    workdir = str(inner.get("WorkingDir") or "/")
    for op in recipe.ops:
        if op.kind == "WORKDIR":
            workdir = _absolute(workdir, op.args[0])
    inner["WorkingDir"] = workdir
    if recipe.user is not None:
        inner["User"] = recipe.user
    if recipe.entrypoint is not None:
        inner["Entrypoint"] = recipe.entrypoint
    if recipe.cmd is not None:
        inner["Cmd"] = recipe.cmd
    elif recipe.clear_cmd:
        inner["Cmd"] = None
    merged = dict(inner.get("Labels") or {})
    merged.update(recipe.labels)
    merged.update(labels)
    inner["Labels"] = merged
    if recipe.exposed:
        ports = dict(inner.get("ExposedPorts") or {})
        ports.update({port: {} for port in recipe.exposed})
        inner["ExposedPorts"] = ports
    config["created"] = _EPOCH
    rootfs = config.setdefault("rootfs", {"type": "layers", "diff_ids": []})
    rootfs["diff_ids"] = [*rootfs.get("diff_ids", []), layer.diff_id]
    history = list(config.get("history") or [])
    history.append({"created": _EPOCH, "created_by": "city deploy: agent source layer"})
    config["history"] = history
    return json.dumps(config, separators=(",", ":"), sort_keys=True).encode("utf-8")


def image_manifest(base: Mapping[str, Any], config: bytes, layer: Layer) -> tuple[bytes, str]:
    media = str(base.get("mediaType") or "application/vnd.oci.image.manifest.v1+json")
    docker = "docker" in media
    layer_type = (
        "application/vnd.docker.image.rootfs.diff.tar.gzip"
        if docker
        else "application/vnd.oci.image.layer.v1.tar+gzip"
    )
    config_type = (
        "application/vnd.docker.container.image.v1+json"
        if docker
        else "application/vnd.oci.image.config.v1+json"
    )
    manifest = {
        "schemaVersion": 2,
        "mediaType": media,
        "config": {"mediaType": config_type, "size": len(config), "digest": _sha256(config)},
        "layers": [
            *base.get("layers", []),
            {"mediaType": layer_type, "size": layer.size, "digest": layer.digest},
        ],
    }
    return json.dumps(manifest, separators=(",", ":")).encode("utf-8"), media


def _env_map(rows: Iterable[object]) -> dict[str, str]:
    found: dict[str, str] = {}
    for row in rows:
        if isinstance(row, str):
            key, _, value = row.partition("=")
            found[key] = value
    return found


def _expand_vars(value: str, env: Mapping[str, str]) -> str:
    return _VAR.sub(lambda match: env.get(match.group(1) or match.group(2), ""), value)


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()
