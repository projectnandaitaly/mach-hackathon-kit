"""Deploy records and their JSON form, shared by the snapshot and the SQL rows."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from cap.core.errors import CapError
from cap.core.keys import PublicKey

STATUSES = frozenset({"accepted", "uploading", "building", "conformance", "promoted", "failed"})


@dataclass
class TeamSession:
    team_id: str
    permissions: frozenset[str]
    controller: PublicKey | None = None


@dataclass
class DeviceChallenge:
    user_code: str
    team: TeamSession | None = None
    # Loopback (browser redirect) login: the token is released only by
    # ``exchange_device`` with the one-time auth code and the PKCE verifier.
    code_challenge: str | None = None
    auth_code: str | None = None


@dataclass
class Deployment:
    deployment_id: str
    team_id: str
    agent_id: str
    version: str
    card: dict[str, Any]
    status: str
    upload_url: str
    created_at: datetime
    runtime_name: str
    promote: bool
    outcome: str | None = None
    image_digest: str | None = None
    sbom_id: str | None = None
    scan: str | None = None
    base_image: str | None = None
    stages: list[dict[str, object]] = field(default_factory=list)
    profiles: list[str] = field(default_factory=list)
    step: str | None = None
    detail: str | None = None

    def view(self) -> dict[str, Any]:
        if self.status not in STATUSES:
            raise CapError("SCHEMA_INVALID", "status")
        body: dict[str, Any] = {
            "deployment_id": self.deployment_id,
            "status": self.status,
            "upload_url": self.upload_url,
            "agent_id": self.agent_id,
            "version": self.version,
        }
        if self.outcome is not None:
            body["outcome"] = self.outcome
        if self.base_image is not None:
            body["base_image"] = self.base_image
        if self.image_digest is not None:
            body["image_digest"] = self.image_digest
        if self.stages:
            body["stages"] = self.stages
        if self.step is not None:
            body["step"] = self.step
        if self.detail is not None:
            body["detail"] = self.detail
        return body


def deployment_blob(dep: Deployment) -> dict[str, object]:
    return {
        "deployment_id": dep.deployment_id,
        "team_id": dep.team_id,
        "agent_id": dep.agent_id,
        "version": dep.version,
        "card": dep.card,
        "status": dep.status,
        "upload_url": dep.upload_url,
        "created_at": dep.created_at.isoformat(),
        "runtime_name": dep.runtime_name,
        "promote": dep.promote,
        "outcome": dep.outcome,
        "image_digest": dep.image_digest,
        "sbom_id": dep.sbom_id,
        "scan": dep.scan,
        "base_image": dep.base_image,
        "stages": list(dep.stages),
        "profiles": list(dep.profiles),
        "step": dep.step,
        "detail": dep.detail,
    }


def deployment_from(value: object) -> Deployment:
    blob = object_map(value, "deployment")
    created = datetime.fromisoformat(text(blob, "created_at"))
    if created.tzinfo is None:
        raise CapError("SCHEMA_INVALID", "created_at")
    status = text(blob, "status")
    if status not in STATUSES:
        raise CapError("SCHEMA_INVALID", "status")
    return Deployment(
        deployment_id=text(blob, "deployment_id"),
        team_id=text(blob, "team_id"),
        agent_id=text(blob, "agent_id"),
        version=text(blob, "version"),
        card=any_map(blob.get("card")),
        status=status,
        upload_url=text(blob, "upload_url"),
        created_at=created,
        runtime_name=text(blob, "runtime_name"),
        promote=flag(blob.get("promote")),
        outcome=opt_text(blob.get("outcome")),
        image_digest=opt_text(blob.get("image_digest")),
        sbom_id=opt_text(blob.get("sbom_id")),
        scan=opt_text(blob.get("scan")),
        base_image=opt_text(blob.get("base_image")),
        stages=stages(blob.get("stages")),
        profiles=texts(blob.get("profiles")),
        step=opt_text(blob.get("step")),
        detail=opt_text(blob.get("detail")),
    )


def session_blob(session: TeamSession) -> dict[str, object]:
    controller = None if session.controller is None else session.controller.to_jwk()
    return {
        "team_id": session.team_id,
        "permissions": sorted(session.permissions),
        "controller": controller,
    }


def session_from(value: object) -> TeamSession:
    blob = object_map(value, "session")
    controller = blob.get("controller")
    return TeamSession(
        team_id=text(blob, "team_id"),
        permissions=frozenset(texts(blob.get("permissions"))),
        controller=None
        if controller is None
        else PublicKey.from_jwk(object_map(controller, "jwk")),
    )


def device_blob(challenge: DeviceChallenge) -> dict[str, object]:
    team = None if challenge.team is None else session_blob(challenge.team)
    return {
        "user_code": challenge.user_code,
        "team": team,
        "code_challenge": challenge.code_challenge,
        "auth_code": challenge.auth_code,
    }


def device_from(value: object) -> DeviceChallenge:
    blob = object_map(value, "device")
    team = blob.get("team")
    return DeviceChallenge(
        user_code=text(blob, "user_code"),
        team=None if team is None else session_from(team),
        code_challenge=opt_text(blob.get("code_challenge")),
        auth_code=opt_text(blob.get("auth_code")),
    )


def object_map(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CapError("SCHEMA_INVALID", label)
    out: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise CapError("SCHEMA_INVALID", label)
        out[key] = item
    return out


def any_map(value: object) -> dict[str, Any]:
    raw = object_map(value, "card")
    return cast(dict[str, Any], raw)


def text(blob: Mapping[str, object], name: str) -> str:
    value = blob.get(name)
    if not isinstance(value, str):
        raise CapError("SCHEMA_INVALID", name)
    return value


def opt_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CapError("SCHEMA_INVALID", "text")
    return value


def flag(value: object) -> bool:
    if not isinstance(value, bool):
        raise CapError("SCHEMA_INVALID", "promote")
    return value


def texts(value: object) -> list[str]:
    if not isinstance(value, list):
        raise CapError("SCHEMA_INVALID", "list")
    found: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise CapError("SCHEMA_INVALID", "list")
        found.append(item)
    return found


def stages(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise CapError("SCHEMA_INVALID", "stages")
    return [object_map(item, "stage") for item in value]
