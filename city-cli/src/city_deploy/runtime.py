"""Runtime port. Memory is the default. ``gke`` applies Autopilot workloads."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import time
import urllib.request
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

import httpx
from cap.core.errors import CapError
from cap.core.http_ports import AGENT_CONTAINER_PORT, Plane, endpoint
from cap.core.keys import generate
from city_conform.agent import Sender, SuiteSigners
from city_index.catalog import assert_city_invoke

from city_deploy.agent_ids import id_name

DRIVERS = frozenset({"memory", "gke", "cloudrun"})
EGRESS = ("relay", "index", "attestation")
_URLS = (
    ("relay", "CITY_RELAY_URL", Plane.RELAY),
    ("index", "CITY_INDEX_URL", Plane.INDEX),
    ("attestation", "CITY_ATTESTATION_URL", Plane.ATTESTATION),
    ("ledger", "CITY_LEDGER_URL", Plane.LEDGER),
)
_LLM_URL = ("llm", "CITY_LLM_URL")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_AR_IMAGE = re.compile(
    r"^[a-z0-9]+(?:[.-][a-z0-9]+)*-docker\.pkg\.dev"
    r"/[a-z0-9][a-z0-9._/-]{0,200}@sha256:[0-9a-f]{64}$"
)
_LOCAL_HOSTS = frozenset({"localhost", "localhost.localdomain", "0.0.0.0", "::1", "127.0.0.1"})
IDENTITY_SECRET = "city-identity"
_KEY_FILE = "agent-key.pem"
_GRANT_FILE = "grant.json"
_TRUST_FILE = "trusted.json"
_LLM_FILE = "llm.vk"
_TRUST_ANNOTATION = "city.projectnandaitaly.ai/trust"
_KEY_MOUNT = "/var/run/city"
# Regional internal Application Load Balancer (GatewayClass gke-l7-rilb).
# Cloud Run reaches this name over VPC peering. It is not a public IP.
INGRESS_HOST = "ingress.agents.internal"
INGRESS_ADDRESS = "city-agent-ingress"
TLS_SECRET = "city-agent-ingress"
PROXY_ONLY_CIDR = "10.49.0.0/23"
# Google health probes for the internal application load balancer.
HEALTH_CHECK_CIDRS = ("35.191.0.0/16", "130.211.0.0/22")
# images/city-sdk creates user city with this uid. The image USER is a name, which the
# kubelet cannot check against runAsNonRoot, so pods state the number.
CITY_UID = 65532
WARM_POOL = "city-warm-pool"
WARM_PRIORITY = "city-warm"
_APPLY_PATCH = "application/apply-patch+yaml"
_FIELD_MANAGER = "city-deploy"
_IMAGE_ERRORS = frozenset(
    {"ErrImagePull", "ImagePullBackOff", "InvalidImageName", "ErrImageNeverPull"}
)
_START_ERRORS = frozenset({"CreateContainerConfigError", "CreateContainerError"})
_LOG_TAIL = 20
_DETAIL_MAX = 2000


class RolloutFailed(Exception):
    """A placed revision did not become ready. ``FAIL`` is the team's, ``ERROR`` the city's."""

    def __init__(self, outcome: str, detail: str) -> None:
        super().__init__(detail)
        self.outcome = outcome
        self.detail = detail


class Runtime(Protocol):
    name: str
    remote: bool
    revisions: dict[str, list[Revision]]
    scales: dict[str, Scale]
    egress: dict[str, tuple[str, ...]]
    drained: set[str]
    smoked: set[str]

    def set_traffic(
        self,
        agent_id: str,
        version: str,
        card: dict[str, Any],
        image_digest: str,
        percent: int,
    ) -> None: ...

    def serving(self, agent_id: str) -> Revision | None: ...


class ClusterClient(Protocol):
    def apply(self, manifest: Mapping[str, Any]) -> int | None: ...


@dataclass
class Revision:
    version: str
    card: dict[str, Any]
    image_digest: str
    traffic: int
    certified: bool = True


@dataclass
class Scale:
    min_replicas: int
    max_replicas: int
    concurrency: int


@dataclass
class MemoryRuntime:
    """Records revisions and traffic. It does not create a cluster or a service."""

    name: str = "memory"
    remote: bool = False
    revisions: dict[str, list[Revision]] = field(default_factory=dict)
    scales: dict[str, Scale] = field(default_factory=dict)
    egress: dict[str, tuple[str, ...]] = field(default_factory=dict)
    drained: set[str] = field(default_factory=set)
    smoked: set[str] = field(default_factory=set)
    removed_namespaces: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.name not in DRIVERS:
            raise CapError("SCHEMA_INVALID", "runtime")
        self.remote = False

    def remove_namespace(self, namespace: str) -> None:
        self.removed_namespaces.add(namespace)

    def set_traffic(
        self,
        agent_id: str,
        version: str,
        card: dict[str, Any],
        image_digest: str,
        percent: int,
    ) -> None:
        rows = self.revisions.setdefault(agent_id, [])
        found = next((row for row in rows if row.version == version), None)
        if found is None:
            found = Revision(
                version=version,
                card=dict(card),
                image_digest=image_digest,
                traffic=percent,
            )
            rows.append(found)
        else:
            found.card = dict(card)
            found.image_digest = image_digest
            found.traffic = percent
            found.certified = True
        if percent == 100:
            for other in rows:
                if other.version != version:
                    other.traffic = 0
            return
        serving = [row for row in rows if row.version != version and row.traffic == 100]
        if len(serving) == 1:
            serving[0].traffic = 100 - percent

    def serving(self, agent_id: str) -> Revision | None:
        rows = self.revisions.get(agent_id, [])
        if not rows:
            return None
        return max(rows, key=lambda row: row.traffic)


class KubernetesClient:
    """Server-side apply of JSON manifests over one kept-alive client. The token is not logged."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        transport: httpx.BaseTransport | None = None,
        token_provider: Callable[[], str] | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._transport = transport
        self._provider = token_provider
        self._http: httpx.Client | None = None

    @classmethod
    def from_env(cls, *, token_path: Path | None = None) -> KubernetesClient:
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
        if not host:
            raise CapError("POLICY_VIOLATION", "kubernetes")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        path = token_path or Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise CapError("POLICY_VIOLATION", "kubernetes") from exc
        if not token:
            raise CapError("POLICY_VIOLATION", "kubernetes")
        return cls(base_url=f"https://{host}:{port}", token=token)

    def apply(self, manifest: Mapping[str, Any]) -> int | None:
        path = f"{_resource_path(manifest)}/{manifest['metadata']['name']}"
        spec = manifest.get("spec")
        partial = (
            manifest.get("kind") == "Deployment"
            and isinstance(spec, Mapping)
            and "template" not in spec
        )
        # A replicas-only Deployment is a scale change. Server-side apply would drop the
        # template that the full manifest owns, so it is a merge patch instead.
        content_type = "application/merge-patch+json" if partial else _APPLY_PATCH
        params = None if partial else {"fieldManager": _FIELD_MANAGER, "force": "true"}
        response = self._client().request(
            "PATCH",
            path,
            params=params,
            content=json.dumps(dict(manifest)).encode("utf-8"),
            headers={"Authorization": self._authorization(), "Content-Type": content_type},
        )
        if response.status_code >= 300:
            detail = _apply_refusal(response)
            raise CapError("POLICY_VIOLATION", f"gke apply {response.status_code}: {detail}")
        try:
            body = response.json()
        except ValueError:
            return None
        if not isinstance(body, dict):
            return None
        metadata = body.get("metadata")
        generation = metadata.get("generation") if isinstance(metadata, dict) else None
        return generation if type(generation) is int else None

    def delete(self, manifest: Mapping[str, Any]) -> None:
        name = manifest["metadata"]["name"]
        path = f"{_resource_path(manifest)}/{name}"
        response = self._client().request(
            "DELETE",
            path,
            params={"propagationPolicy": "Background"},
            headers={"Authorization": self._authorization()},
        )
        # 404: already gone. 409: Namespace still Terminating after a prior purge.
        if response.status_code in {200, 202, 404}:
            return
        if response.status_code == 409 and manifest.get("kind") == "Namespace":
            return
        raise CapError("POLICY_VIOLATION", f"gke delete {response.status_code}")

    def get(self, path: str, params: Mapping[str, str] | None = None) -> dict[str, Any] | None:
        response = self._client().get(
            path, params=params, headers={"Authorization": self._authorization()}
        )
        if response.status_code == 404:
            return None
        if response.status_code >= 300:
            raise CapError("POLICY_VIOLATION", f"gke get {response.status_code}")
        body = response.json()
        return body if isinstance(body, dict) else None

    def read_log(self, namespace: str, pod: str, *, previous: bool, tail: int) -> str:
        response = self._client().get(
            f"/api/v1/namespaces/{namespace}/pods/{pod}/log",
            params={"previous": "true" if previous else "false", "tailLines": str(tail)},
            headers={"Authorization": self._authorization()},
        )
        if response.status_code >= 300:
            return ""
        return response.text

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                transport=self._transport,
                base_url=self._base,
                timeout=httpx.Timeout(15.0, connect=5.0),
            )
        return self._http

    def _authorization(self) -> str:
        token = self._provider() if self._provider is not None else self._token
        if not token:
            raise CapError("POLICY_VIOLATION", "kubernetes")
        return f"Bearer {token}"


class _ScaleMap(dict[str, Scale]):
    def __init__(self, runtime: GkeRuntime) -> None:
        super().__init__()
        self._runtime = runtime

    def __setitem__(self, key: str, value: Scale) -> None:
        super().__setitem__(key, value)
        self._runtime.apply_scale(key)


class _DrainSet(set[str]):
    def __init__(self, runtime: GkeRuntime) -> None:
        super().__init__()
        self._runtime = runtime

    def add(self, element: str) -> None:
        super().add(element)
        self._runtime.quiesce(element)


class GkeRuntime:
    """Autopilot workloads: gVisor, no API token, digest pin, city-only egress."""

    def __init__(
        self,
        client: ClusterClient,
        *,
        repository: str = "city-agent",
        urls: Mapping[str, str] | None = None,
        ingress_host: str | None = None,
        environ: Mapping[str, str] | None = None,
        identity: Callable[[str], Mapping[str, str] | None] | None = None,
        probe: Callable[[str], Awaitable[tuple[int, str | None]]] | None = None,
        poll_interval: float = 1.0,
        ready_timeout: float = 300.0,
        route_timeout: float = 240.0,
        route_settle: int = 3,
    ) -> None:
        self.name = "gke"
        self.remote = True
        self._book = MemoryRuntime("gke")
        self.client = client
        self.repository = repository
        self._identity = identity
        self._llm_key: Callable[[str], str | None] | None = None
        self._probe = probe or _http_health
        self._poll = poll_interval
        self._ready_timeout = ready_timeout
        self._route_timeout = route_timeout
        self._route_settle = route_settle
        source = os.environ if environ is None else environ
        self._cloud = source.get("CITY_ENV") == "cloud"
        self._warm = _warm_rows(source)
        self._shared_applied = False
        host = (ingress_host or source.get("CITY_AGENT_INGRESS_HOST") or INGRESS_HOST).strip()
        assert_city_invoke(f"https://{host}/health")
        self.ingress_host = host
        self.urls = _resolve_urls(source, urls, cloud=self._cloud)
        self._egress = _destination_rules(source.get("CITY_EGRESS_CIDRS", ""), self.urls)
        self.revisions = self._book.revisions
        self.scales: dict[str, Scale] = _ScaleMap(self)
        self.egress = self._book.egress
        self.drained: set[str] = _DrainSet(self)
        self.smoked = self._book.smoked
        self._pinned: dict[str, str] = {}
        self._placed: dict[str, tuple[str, str, str]] = {}
        self._roll_generation: dict[str, int] = {}
        self._suite = suite_signers()
        self._trusted = trusted_keys(self._suite)

    def suite_keys(self) -> SuiteSigners:
        """Senders whose public keys this runtime wrote into the agent's identity secret."""

        return self._suite

    def bind_identity(self, source: Callable[[str], Mapping[str, str] | None]) -> None:
        self._identity = source

    def bind_llm_key(self, source: Callable[[str], str | None]) -> None:
        """Gateway key by team slug, read at roll-out from the card's verified ``team``."""

        self._llm_key = source

    def set_traffic(
        self,
        agent_id: str,
        version: str,
        card: dict[str, Any],
        image_digest: str,
        percent: int,
    ) -> None:
        self._book.set_traffic(agent_id, version, card, image_digest, percent)
        if self._cloud and not self._egress:
            raise CapError("POLICY_VIOLATION", "CITY_EGRESS_CIDRS")
        image = pin_image(self.repository, image_digest)
        files = None if self._identity is None else _identity_files(self._identity(agent_id))
        if self._cloud and files is None:
            raise CapError("POLICY_VIOLATION", "agent identity")
        namespace = namespace_for(str(card.get("team", "team:unknown")))
        slug = dns_name(id_name(agent_id))
        service = slug if percent == 100 else f"{slug}-candidate"
        self._placed[agent_id] = (namespace, service, version)
        replicas = 0 if percent == 0 or agent_id in self.drained else self._replicas(agent_id)
        rows = workloads(
            agent_id=agent_id,
            version=version,
            namespace=namespace,
            service_name=service,
            image=image,
            replicas=replicas,
            urls=self.urls,
            ingress_host=self.ingress_host,
            egress=self._egress,
        )
        if files is not None:
            key, grant = files
            team = str(card.get("team", "")).removeprefix("team:")
            llm_key = self._llm_key(team) if self._llm_key is not None and team else None
            secret = identity_secret(
                namespace,
                key,
                grant,
                self._trusted,
                name=identity_name(service, version),
                llm_key=llm_key,
            )
            at = next(index for index, row in enumerate(rows) if row.get("kind") == "Deployment")
            _annotate_trust(rows[at], self._trusted)
            rows.insert(at, secret)
        shared = [row for row in rows if _is_shared(row)]
        own = [row for row in rows if not _is_shared(row)]
        if not self._shared_applied:
            for manifest in [*shared, *self._warm]:
                self.client.apply(manifest)
            self._shared_applied = True
        for manifest in own:
            if manifest.get("kind") == "Deployment" and isinstance(manifest.get("spec"), dict):
                spec = manifest["spec"]
                manifest["metadata"]["name"] = workload_name(service, version)
                if percent == 100:
                    spec["strategy"] = {
                        "type": "RollingUpdate",
                        "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0},
                    }
                if "template" in spec:
                    _stamp_placement(manifest)
            applied = self.client.apply(manifest)
            if manifest.get("kind") == "Deployment" and type(applied) is int:
                self._roll_generation[agent_id] = applied

    async def wait_ready(self, agent_id: str) -> None:
        """Pods ready, then ``/health`` through the ingress route. Fails fast on known errors."""

        read = getattr(self.client, "get", None)
        placed = self._placed.get(agent_id)
        if not callable(read) or placed is None:
            return
        namespace, service, version = placed
        name = workload_name(service, version)
        path = f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}"
        deadline = time.monotonic() + self._ready_timeout
        while True:
            found = await asyncio.to_thread(read, path)
            if found is not None and _wanted(found) == 0:
                return
            if _roll_caught_up(found, self._roll_generation.get(agent_id)):
                break
            pods = await asyncio.to_thread(
                read, f"/api/v1/namespaces/{namespace}/pods", {"labelSelector": f"app={service}"}
            )
            problem = _pod_problem(pods, name)
            if problem is not None:
                outcome, detail, pod = problem
                if outcome == "FAIL":
                    detail += await self._log_tail(namespace, pod)
                raise RolloutFailed(outcome, detail[:_DETAIL_MAX])
            if time.monotonic() >= deadline:
                raise RolloutFailed("ERROR", "agent pods did not become ready in time")
            await asyncio.sleep(self._poll)
        url = self.candidate_url(agent_id) + "/health"
        deadline = time.monotonic() + self._route_timeout
        # The load balancer's proxies take a new route one by one, and for a while they can
        # still reach the previous pod, so only consecutive answers from this version count.
        healthy = 0
        while True:
            status, served = await self._probe(url)
            healthy = healthy + 1 if status == 200 and served == version else 0
            if healthy >= self._route_settle:
                return
            if time.monotonic() >= deadline:
                raise RolloutFailed(
                    "ERROR",
                    "agent ingress route did not become ready in time "
                    f"(last {status}, version {(served or '-')[:40]!r}, expected {version})",
                )
            await asyncio.sleep(self._poll)

    async def scale_down_superseded(self, agent_id: str) -> list[str]:
        """Delete every other Deployment of the agent. A client without delete scales them to zero.

        Returns the names removed.
        """

        read = getattr(self.client, "get", None)
        placed = self._placed.get(agent_id)
        if not callable(read) or placed is None:
            return []
        namespace, service, version = placed
        keep = workload_name(service, version)
        slug = dns_name(id_name(agent_id))
        apps = {slug, dns_name(f"{slug}-candidate")}
        listed = await asyncio.to_thread(read, f"/apis/apps/v1/namespaces/{namespace}/deployments")
        items = listed.get("items") if isinstance(listed, Mapping) else None
        scaled: list[str] = []
        for item in items if isinstance(items, list) else []:
            name = str((item.get("metadata") or {}).get("name", ""))
            spec = item.get("spec") or {}
            app = ((spec.get("selector") or {}).get("matchLabels") or {}).get("app")
            delete = getattr(self.client, "delete", None)
            if not name or name == keep or app not in apps:
                continue
            if spec.get("replicas") == 0 and not callable(delete):
                continue
            if callable(delete):
                await asyncio.to_thread(
                    delete,
                    {
                        "apiVersion": "apps/v1",
                        "kind": "Deployment",
                        "metadata": {"name": name, "namespace": namespace},
                    },
                )
            else:
                await asyncio.to_thread(
                    self.client.apply,
                    {
                        "apiVersion": "apps/v1",
                        "kind": "Deployment",
                        "metadata": {"name": name, "namespace": namespace},
                        "spec": {"replicas": 0},
                    },
                )
            scaled.append(name)
        return scaled

    async def _log_tail(self, namespace: str, pod: str) -> str:
        read_log = getattr(self.client, "read_log", None)
        if not callable(read_log) or not pod:
            return ""
        text = await asyncio.to_thread(read_log, namespace, pod, previous=True, tail=_LOG_TAIL)
        lines = [line for line in str(text).splitlines() if "BEGIN" not in line]
        return "\n" + "\n".join(lines[-_LOG_TAIL:]) if lines else ""

    def serving(self, agent_id: str) -> Revision | None:
        return self._book.serving(agent_id)

    def evidence_url(self, agent_id: str) -> str:
        """Attestation base URL. CandidateSuite loads ``GET /v1/evidence`` there."""

        del agent_id
        return self.urls["attestation"]

    def index_url(self, agent_id: str) -> str:
        """Index base URL. The remote suite reads the agent's own view of the market there."""

        del agent_id
        return self.urls["index"]

    async def open_conformance(self, payload: Mapping[str, Any]) -> None:
        """Lease the suite message on the Relay before the candidate renews it.

        A payload the agent must reject as malformed is not a delivery, so it is not leased.
        """

        if conformance_fields(payload) is None:
            return
        await post_conformance(
            self.urls["relay"],
            payload,
            token=_metadata_id_token(self.urls["relay"]),
        )

    def candidate_url(self, agent_id: str) -> str:
        pinned = self._pinned.get(agent_id)
        if pinned is not None:
            return pinned
        placed = self._placed.get(agent_id)
        if placed is None:
            raise CapError("TARGET_NOT_VISIBLE", agent_id)
        namespace, service, _version = placed
        return assert_city_invoke(f"https://{self.ingress_host}/{namespace}/{service}")

    def pin_url(self, agent_id: str, url: str) -> None:
        self._pinned[agent_id] = assert_city_invoke(url)

    def adopt(self, agent_id: str) -> None:
        """Point scale and quiesce at the serving workload of a restored book. Applies nothing."""

        serving = self._book.serving(agent_id)
        if serving is None or serving.traffic == 0:
            return
        namespace = namespace_for(str(serving.card.get("team", "team:unknown")))
        slug = dns_name(id_name(agent_id))
        self._placed[agent_id] = (namespace, slug, serving.version)

    def apply_scale(self, agent_id: str) -> None:
        placed = self._placed.get(agent_id)
        if placed is None:
            return
        namespace, service, version = placed
        self.client.apply(
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": workload_name(service, version), "namespace": namespace},
                "spec": {"replicas": self._replicas(agent_id)},
            }
        )

    def remove(self, agent_id: str) -> None:
        """Delete the Deployment just placed. Background propagation removes its pods."""

        placed = self._placed.get(agent_id)
        if placed is None:
            return
        namespace, service, version = placed
        manifest = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": workload_name(service, version),
                "namespace": namespace,
            },
        }
        delete = getattr(self.client, "delete", None)
        if callable(delete):
            delete(manifest)
            return
        self.quiesce(agent_id)

    def remove_namespace(self, namespace: str) -> None:
        """Delete a team namespace and everything in it. Nothing placed there is scaled again."""

        if not namespace.startswith("team-"):
            raise CapError("POLICY_VIOLATION", "namespace")
        for agent_id, placed in list(self._placed.items()):
            if placed[0] == namespace:
                del self._placed[agent_id]
        delete = getattr(self.client, "delete", None)
        if not callable(delete):
            raise CapError("POLICY_VIOLATION", "gke delete")
        delete({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}})

    def quiesce(self, agent_id: str) -> None:
        placed = self._placed.get(agent_id)
        if placed is None:
            return
        namespace, service, version = placed
        self.client.apply(
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": workload_name(service, version), "namespace": namespace},
                "spec": {"replicas": 0},
            }
        )

    def remove_agent(self, agent_id: str) -> None:
        """Delete every Deployment of the agent, serving and candidate."""

        placed = self._placed.get(agent_id)
        if placed is None:
            self.remove(agent_id)
            return
        namespace = placed[0]
        slug = dns_name(id_name(agent_id))
        apps = {slug, dns_name(f"{slug}-candidate")}
        read = getattr(self.client, "get", None)
        delete = getattr(self.client, "delete", None)
        if not callable(read) or not callable(delete):
            self.remove(agent_id)
            return
        listed = read(f"/apis/apps/v1/namespaces/{namespace}/deployments")
        items = listed.get("items") if isinstance(listed, Mapping) else None
        for item in items if isinstance(items, list) else []:
            name = str((item.get("metadata") or {}).get("name", ""))
            spec = item.get("spec") or {}
            app = ((spec.get("selector") or {}).get("matchLabels") or {}).get("app")
            if name and app in apps:
                delete(
                    {
                        "apiVersion": "apps/v1",
                        "kind": "Deployment",
                        "metadata": {"name": name, "namespace": namespace},
                    }
                )
        self._placed.pop(agent_id, None)

    def _replicas(self, agent_id: str) -> int:
        scale = self.scales.get(agent_id)
        if scale is None:
            return 1
        return scale.min_replicas


def _resource_path(manifest: Mapping[str, Any]) -> str:
    kind = str(manifest.get("kind", ""))
    api = str(manifest.get("apiVersion", ""))
    metadata = manifest.get("metadata")
    namespace = ""
    if isinstance(metadata, Mapping):
        raw = metadata.get("namespace")
        if isinstance(raw, str):
            namespace = raw
    if kind == "Namespace":
        return "/api/v1/namespaces"
    if kind == "ServiceAccount":
        return f"/api/v1/namespaces/{namespace}/serviceaccounts"
    if kind == "Secret":
        return f"/api/v1/namespaces/{namespace}/secrets"
    if kind == "Service":
        return f"/api/v1/namespaces/{namespace}/services"
    if kind == "NetworkPolicy" and api == "networking.k8s.io/v1":
        return f"/apis/networking.k8s.io/v1/namespaces/{namespace}/networkpolicies"
    if kind == "Deployment" and api == "apps/v1":
        return f"/apis/apps/v1/namespaces/{namespace}/deployments"
    if kind == "Gateway" and api == "gateway.networking.k8s.io/v1":
        return f"/apis/gateway.networking.k8s.io/v1/namespaces/{namespace}/gateways"
    if kind == "HTTPRoute" and api == "gateway.networking.k8s.io/v1":
        return f"/apis/gateway.networking.k8s.io/v1/namespaces/{namespace}/httproutes"
    if kind == "HealthCheckPolicy" and api == "networking.gke.io/v1":
        return f"/apis/networking.gke.io/v1/namespaces/{namespace}/healthcheckpolicies"
    if kind == "PriorityClass" and api == "scheduling.k8s.io/v1":
        return "/apis/scheduling.k8s.io/v1/priorityclasses"
    raise CapError("SCHEMA_INVALID", "manifest")


def canonical_image(value: str) -> str:
    """Bare ``sha256:`` digest, or a digest-pinned Artifact Registry image."""

    text = value.strip()
    if _DIGEST.fullmatch(text):
        return text
    if _AR_IMAGE.fullmatch(text) and ".." not in text and "//" not in text:
        return text
    raise CapError("SCHEMA_INVALID", "image_digest")


def pin_image(repository: str, image_ref: str) -> str:
    text = canonical_image(image_ref)
    if not _DIGEST.fullmatch(text):
        return text
    repo = repository.strip()
    bad = any(char in repo for char in " @:\n\t") or ".." in repo or repo.startswith("http")
    if not repo or bad:
        raise CapError("SCHEMA_INVALID", "image_digest")
    return f"{repo}@{text}"


def namespace_for(team: str) -> str:
    return dns_name(team.replace(":", "-"))


def deployment_name(service: str, version: str) -> str:
    return dns_name(f"{service}-{version.replace('.', '-')}")


def workload_name(service: str, version: str) -> str:
    """The serving Deployment is the agent slug. A candidate keeps the version in its name."""

    if service.endswith("-candidate"):
        return deployment_name(service, version)
    return service


def identity_name(service: str, version: str) -> str:
    """The Secret one Deployment mounts. A pod reads it once at boot, so it is never shared."""

    return f"{deployment_name(service, version)}-identity"


def dns_name(value: str) -> str:
    """A Kubernetes DNS-1035 label (Service, HTTPRoute, and HealthCheckPolicy names).

    Minted rids (ADR-085) are Crockford base32 and may start with a digit; Service
    names may not, so a leading digit is prefixed with ``a``.
    """

    cleaned = "".join(char if char.isalnum() or char == "-" else "-" for char in value.lower())
    cleaned = cleaned.strip("-")[:63]
    if not cleaned:
        raise CapError("SCHEMA_INVALID", "name")
    if cleaned[0].isdigit():
        cleaned = ("a" + cleaned)[:63]
    return cleaned


def workloads(
    *,
    agent_id: str,
    version: str,
    namespace: str,
    service_name: str,
    image: str,
    replicas: int,
    urls: Mapping[str, str],
    ingress_host: str = INGRESS_HOST,
    egress: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    del agent_id
    account = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"name": "agent", "namespace": namespace},
        "automountServiceAccountToken": False,
    }
    namespace_manifest = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": namespace,
            "labels": {"city.milano/team": namespace},
        },
    }
    deny = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "default-deny", "namespace": namespace},
        "spec": {
            "podSelector": {},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [],
            "egress": [],
        },
    }
    allow = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "city-only", "namespace": namespace},
        "spec": {
            "podSelector": {},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [
                {
                    "from": [
                        {"ipBlock": {"cidr": cidr}}
                        for cidr in (PROXY_ONLY_CIDR, *HEALTH_CHECK_CIDRS)
                    ],
                    "ports": [{"protocol": "TCP", "port": AGENT_CONTAINER_PORT}],
                }
            ],
            "egress": [_dns_egress(), *egress],
        },
    }
    deploy = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": deployment_name(service_name, version),
            "namespace": namespace,
        },
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"app": service_name}},
            "template": {
                "metadata": {"labels": {"app": service_name, "version": version}},
                "spec": {
                    "runtimeClassName": "gvisor",
                    "serviceAccountName": "agent",
                    "automountServiceAccountToken": False,
                    "containers": [
                        {
                            "name": "agent",
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "ports": [{"containerPort": AGENT_CONTAINER_PORT}],
                            "readinessProbe": {
                                "httpGet": {"path": "/health", "port": AGENT_CONTAINER_PORT},
                                "periodSeconds": 2,
                                "timeoutSeconds": 2,
                                "failureThreshold": 3,
                            },
                            "env": [
                                {"name": "CITY_ENV", "value": "cloud"},
                                {"name": "PORT", "value": str(AGENT_CONTAINER_PORT)},
                                {"name": "CITY_RELAY_URL", "value": urls["relay"]},
                                {"name": "CITY_INDEX_URL", "value": urls["index"]},
                                {"name": "CITY_ATTESTATION_URL", "value": urls["attestation"]},
                                {"name": "CITY_LEDGER_URL", "value": urls["ledger"]},
                                {"name": "CITY_KEY_PATH", "value": f"{_KEY_MOUNT}/{_KEY_FILE}"},
                                {"name": "CITY_GRANT_PATH", "value": f"{_KEY_MOUNT}/{_GRANT_FILE}"},
                                {
                                    "name": "CITY_TRUSTED_KEYS_PATH",
                                    "value": f"{_KEY_MOUNT}/{_TRUST_FILE}",
                                },
                                {"name": "CITY_IMAGE_DIGEST", "value": image.rsplit("@", 1)[-1]},
                                *_llm_env(urls),
                            ],
                            "volumeMounts": [
                                {
                                    "name": IDENTITY_SECRET,
                                    "mountPath": _KEY_MOUNT,
                                    "readOnly": True,
                                }
                            ],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "runAsNonRoot": True,
                                "runAsUser": CITY_UID,
                                "runAsGroup": CITY_UID,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ],
                    "volumes": [
                        {
                            "name": IDENTITY_SECRET,
                            "secret": {"secretName": identity_name(service_name, version)},
                        }
                    ],
                },
            },
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": service_name,
            "namespace": namespace,
            "annotations": {"cloud.google.com/neg": '{"ingress": true}'},
        },
        "spec": {
            "type": "ClusterIP",
            # version keeps the previous Deployment out of this Service. Both
            # reads of the card must hit the same process.
            "selector": {"app": service_name, "version": version},
            "ports": [{"port": AGENT_CONTAINER_PORT, "targetPort": AGENT_CONTAINER_PORT}],
        },
    }
    city_system = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": "city-system",
            "labels": {
                "city.milano/plane": "control",
                "pni.city/system": "true",
            },
        },
    }
    gateway = {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "Gateway",
        "metadata": {"name": "city-agents", "namespace": "city-system"},
        "spec": {
            "gatewayClassName": "gke-l7-rilb",
            "addresses": [{"type": "NamedAddress", "value": INGRESS_ADDRESS}],
            "listeners": [
                {
                    "name": "https",
                    "protocol": "HTTPS",
                    "port": 443,
                    "tls": {
                        "mode": "Terminate",
                        "certificateRefs": [{"kind": "Secret", "name": TLS_SECRET}],
                    },
                    "allowedRoutes": {"namespaces": {"from": "All"}},
                }
            ],
        },
    }
    route = {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "HTTPRoute",
        "metadata": {"name": service_name, "namespace": namespace},
        "spec": {
            "parentRefs": [
                {"name": "city-agents", "namespace": "city-system", "sectionName": "https"}
            ],
            "hostnames": [ingress_host],
            "rules": [
                {
                    "matches": [
                        {"path": {"type": "PathPrefix", "value": f"/{namespace}/{service_name}"}}
                    ],
                    "filters": [
                        {
                            "type": "URLRewrite",
                            "urlRewrite": {
                                "path": {
                                    "type": "ReplacePrefixMatch",
                                    "replacePrefixMatch": "/",
                                }
                            },
                        }
                    ],
                    "backendRefs": [{"name": service_name, "port": AGENT_CONTAINER_PORT}],
                }
            ],
        },
    }
    # The Gateway health-checks "/" unless told otherwise; the harness serves /health only.
    health = {
        "apiVersion": "networking.gke.io/v1",
        "kind": "HealthCheckPolicy",
        "metadata": {"name": service_name, "namespace": namespace},
        "spec": {
            "default": {
                "checkIntervalSec": 5,
                "timeoutSec": 2,
                "healthyThreshold": 1,
                "unhealthyThreshold": 2,
                "config": {
                    "type": "HTTP",
                    "httpHealthCheck": {"port": AGENT_CONTAINER_PORT, "requestPath": "/health"},
                },
            },
            "targetRef": {"group": "", "kind": "Service", "name": service_name},
        },
    }
    return [
        city_system,
        namespace_manifest,
        account,
        deny,
        allow,
        deploy,
        service,
        gateway,
        route,
        health,
    ]


def warm_pool(image: str, replicas: int) -> list[dict[str, Any]]:
    """Low-priority sandbox pods on the base image: spare gVisor capacity with layers pulled."""

    priority = {
        "apiVersion": "scheduling.k8s.io/v1",
        "kind": "PriorityClass",
        "metadata": {"name": WARM_PRIORITY},
        "value": -10,
        "preemptionPolicy": "Never",
        "globalDefault": False,
        "description": "Placeholder pods that agent pods preempt.",
    }
    pool = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": WARM_POOL, "namespace": "city-system"},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"app": WARM_POOL}},
            "template": {
                "metadata": {"labels": {"app": WARM_POOL}},
                "spec": {
                    "priorityClassName": WARM_PRIORITY,
                    "runtimeClassName": "gvisor",
                    "automountServiceAccountToken": False,
                    "terminationGracePeriodSeconds": 0,
                    "containers": [
                        {
                            "name": "hold",
                            "image": image,
                            "command": ["sleep", "infinity"],
                            "resources": {"requests": {"cpu": "500m", "memory": "2Gi"}},
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "runAsNonRoot": True,
                                "runAsUser": CITY_UID,
                                "runAsGroup": CITY_UID,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ],
                },
            },
        },
    }
    return [priority, pool]


def _dns_egress() -> dict[str, Any]:
    # GKE Autopilot puts NodeLocal DNSCache on 169.254.20.10. Pods use that
    # address, not the kube-dns ClusterIP, so the selector alone is not enough.
    return {
        "to": [
            {
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": "kube-system"},
                },
                "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
            },
            {"ipBlock": {"cidr": "169.254.20.10/32"}},
        ],
        "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
    }


def identity_secret(
    namespace: str,
    key_pem: str,
    grant_json: str,
    trusted_json: str | None = None,
    *,
    name: str,
    llm_key: str | None = None,
) -> dict[str, Any]:
    data = {_KEY_FILE: key_pem, _GRANT_FILE: grant_json}
    if trusted_json is not None:
        data[_TRUST_FILE] = trusted_json
    if llm_key:
        data[_LLM_FILE] = llm_key
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace},
        "type": "Opaque",
        "stringData": data,
    }


def suite_signers() -> SuiteSigners:
    """Deploy-held conformance senders. Only their public keys reach the agent pod.

    The stranger stays unknown to the agent so the unknown-key stage still sees 401.
    """

    def sender(name: str) -> Sender:
        return name, generate().private

    return SuiteSigners(
        sender=sender("agent:t05/osp-x"),
        prober=sender("city:prober"),
        city=sender("city:governance"),
        stranger=sender("agent:t99/stranger"),
    )


def trusted_keys(signers: SuiteSigners) -> str:
    rows = []
    for name, private in (signers.sender, signers.prober, signers.city):
        jwk = private.public_key().to_jwk()
        rows.append({"kid": f"{name}#k1", "kty": jwk["kty"], "crv": jwk["crv"], "x": jwk["x"]})
    return json.dumps({"keys": rows}, sort_keys=True)


def conformance_fields(payload: Mapping[str, Any]) -> dict[str, object] | None:
    delivery = payload.get("delivery")
    envelope = payload.get("envelope")
    if not isinstance(delivery, Mapping) or not isinstance(envelope, Mapping):
        return None
    msg_id = delivery.get("msg_id")
    fence = delivery.get("fence")
    recipient = envelope.get("to")
    if not isinstance(msg_id, str) or type(fence) is not int or not isinstance(recipient, str):
        return None
    return {"msg_id": msg_id, "recipient": recipient, "fence": fence}


async def post_conformance(
    relay_url: str,
    payload: Mapping[str, Any],
    *,
    token: str,
    client: httpx.AsyncClient | None = None,
) -> None:
    fields = conformance_fields(payload)
    if fields is None:
        raise CapError("SCHEMA_INVALID", "delivery")
    msg_id = fields["msg_id"]
    fence = fields["fence"]
    recipient = fields["recipient"]
    owns = client is None
    http = client or httpx.AsyncClient(timeout=5.0)
    try:
        response = await http.post(
            f"{relay_url.rstrip('/')}/v1/markets/m-1/conformance/deliveries",
            json={"msg_id": msg_id, "recipient": recipient, "fence": fence},
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        if owns:
            await http.aclose()
    if response.status_code >= 400:
        raise CapError("POLICY_VIOLATION", "conformance delivery")


def _metadata_id_token(audience: str) -> str:
    url = (
        "http://metadata.google.internal/computeMetadata/v1/"
        "instance/service-accounts/default/identity?audience=" + quote(audience, safe="")
    )
    request = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            raw = response.read()
            token = raw.decode("ascii").strip() if isinstance(raw, bytes) else ""
    except OSError as exc:
        raise CapError("POLICY_VIOLATION", "deploy token") from exc
    if not token:
        raise CapError("POLICY_VIOLATION", "deploy token")
    return token


def _stamp_placement(deployment: dict[str, Any]) -> None:
    """A new token rolls the pods, so conformance never meets the previous process."""

    template = deployment["spec"]["template"]
    annotations = template.setdefault("metadata", {}).setdefault("annotations", {})
    annotations["city.projectnandaitaly.ai/placement"] = uuid.uuid4().hex


def _annotate_trust(deployment: dict[str, Any], trusted_json: str) -> None:
    """Pods read trusted keys at boot, so a new trust set must roll the pods."""

    template = deployment["spec"]["template"]
    annotations = template.setdefault("metadata", {}).setdefault("annotations", {})
    annotations[_TRUST_ANNOTATION] = hashlib.sha256(trusted_json.encode()).hexdigest()[:16]


def _is_shared(row: Mapping[str, Any]) -> bool:
    metadata = row.get("metadata")
    name = metadata.get("name") if isinstance(metadata, Mapping) else None
    return row.get("kind") == "Gateway" or (
        row.get("kind") == "Namespace" and name == "city-system"
    )


def _warm_rows(environ: Mapping[str, str]) -> list[dict[str, Any]]:
    raw = environ.get("CITY_WARM_POOL", "").strip()
    image = environ.get("CITY_SDK_IMAGE", "").strip()
    if not raw or not image:
        return []
    try:
        replicas = int(raw)
    except ValueError as exc:
        raise CapError("SCHEMA_INVALID", "CITY_WARM_POOL") from exc
    if not 0 <= replicas <= 10:
        raise CapError("SCHEMA_INVALID", "CITY_WARM_POOL")
    if replicas == 0:
        return []
    return warm_pool(canonical_image(image), replicas)


def _wanted(found: Mapping[str, Any] | None) -> int:
    spec = found.get("spec") if isinstance(found, Mapping) else None
    if not isinstance(spec, Mapping):
        return 1
    return int(spec.get("replicas", 1))


def _roll_caught_up(found: Mapping[str, Any] | None, generation: int | None) -> bool:
    """Ready only after the controller has observed the Deployment this placement applied."""

    if not _deployment_ready(found):
        return False
    if generation is None or not isinstance(found, Mapping):
        return True
    status = found.get("status") or {}
    if not isinstance(status, Mapping):
        return False
    return int(status.get("observedGeneration", 0)) >= generation


def _deployment_ready(found: Mapping[str, Any] | None) -> bool:
    if not isinstance(found, Mapping):
        return False
    want = _wanted(found)
    if want == 0:
        return True
    status = found.get("status") or {}
    metadata = found.get("metadata") or {}
    if int(status.get("observedGeneration", 0)) < int(metadata.get("generation", 0)):
        return False
    updated = int(status.get("updatedReplicas", 0))
    ready = int(status.get("readyReplicas", 0))
    # replicas above updated means the previous pod is still in the Service.
    return updated >= want and ready >= want and int(status.get("replicas", want)) <= updated


def _apply_refusal(response: httpx.Response) -> str:
    """Short Kubernetes Status message from a failed apply, for the deployment detail."""

    try:
        body = response.json()
    except ValueError:
        return (response.text or "unreadable body")[:300]
    if not isinstance(body, Mapping):
        return "unreadable body"
    message = body.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()[:500]
    details = body.get("details")
    causes = details.get("causes") if isinstance(details, Mapping) else None
    if isinstance(causes, list):
        parts = [
            str(row.get("message", "")).strip()
            for row in causes
            if isinstance(row, Mapping) and row.get("message")
        ]
        if parts:
            return "; ".join(parts)[:500]
    return "no detail"


def _pod_problem(pods: Mapping[str, Any] | None, deployment: str) -> tuple[str, str, str] | None:
    items = pods.get("items") if isinstance(pods, Mapping) else None
    for pod in items if isinstance(items, list) else []:
        name = str((pod.get("metadata") or {}).get("name", ""))
        if not name.startswith(deployment + "-"):
            continue
        for row in (pod.get("status") or {}).get("containerStatuses") or []:
            waiting = (row.get("state") or {}).get("waiting") or {}
            reason = str(waiting.get("reason", ""))
            message = str(waiting.get("message", ""))[:300]
            if reason in _IMAGE_ERRORS:
                return "ERROR", f"image pull failed: {reason} {message}".strip(), name
            if reason in _START_ERRORS:
                return "ERROR", f"container did not start: {reason} {message}".strip(), name
            last = (row.get("lastState") or {}).get("terminated") or {}
            if int(row.get("restartCount", 0)) >= 1 and last:
                code = last.get("exitCode")
                why = str(last.get("reason", "")).strip()
                return (
                    "FAIL",
                    f"agent exited: exit code {code}" + (f" ({why})" if why else ""),
                    name,
                )
            if reason == "CrashLoopBackOff":
                return "FAIL", "agent container is crash looping", name
    return None


def _agent_tls_verify() -> bool | str:
    """CA PEM from Secret Manager when set; otherwise skip public PKI (local/tests)."""

    path = os.environ.get("CITY_AGENT_CA_FILE", "").strip()
    return path if path else False


async def _http_health(url: str) -> tuple[int, str | None]:
    try:
        async with httpx.AsyncClient(timeout=3.0, verify=_agent_tls_verify()) as client:
            response = await client.get(url)
    except httpx.HTTPError:
        return 0, None
    try:
        body: object = response.json()
    except ValueError:
        body = None
    return response.status_code, served_version(body)


def served_version(body: object) -> str | None:
    """The ``version`` an agent's ``GET /health`` reports (``specs/openapi/agent.yaml``)."""

    version = body.get("version") if isinstance(body, Mapping) else None
    return version if isinstance(version, str) else None


def _identity_files(material: Mapping[str, str] | None) -> tuple[str, str] | None:
    if material is None:
        return None
    key = material.get("key", "")
    grant = material.get("grant", "")
    if not key or not grant:
        return None
    return key, grant


def _llm_env(urls: Mapping[str, str]) -> list[dict[str, str]]:
    """Where the brain reaches the Gateway. The key itself stays in the mounted Secret."""

    url = urls.get(_LLM_URL[0])
    if not url:
        return []
    return [
        {"name": "CITY_LLM_URL", "value": url},
        {"name": "CITY_LLM_KEY_PATH", "value": f"{_KEY_MOUNT}/{_LLM_FILE}"},
    ]


def _resolve_urls(
    environ: Mapping[str, str],
    overrides: Mapping[str, str] | None,
    *,
    cloud: bool,
) -> dict[str, str]:
    found: dict[str, str] = {}
    for key, env_name, plane in _URLS:
        chosen = ""
        if overrides is not None and overrides.get(key):
            chosen = overrides[key].strip()
        elif environ.get(env_name, "").strip():
            chosen = environ[env_name].strip()
        if cloud:
            if not chosen:
                raise CapError("POLICY_VIOLATION", env_name)
            found[key] = _cloud_url(chosen, env_name)
        else:
            found[key] = chosen or endpoint(plane)
    key, env_name = _LLM_URL
    if overrides is not None and overrides.get(key):
        llm = overrides[key].strip()
    else:
        llm = environ.get(env_name, "").strip()
    if llm:
        found[key] = _cloud_url(llm, env_name) if cloud else llm.rstrip("/")
    return found


def _cloud_url(url: str, name: str) -> str:
    parsed = urlsplit(url.strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or _is_local_url(url)
    ):
        raise CapError("POLICY_VIOLATION", name)
    return url.strip().rstrip("/")


def _is_local_url(url: str) -> bool:
    host = (urlsplit(url.strip()).hostname or "").strip("[]").lower()
    if host in _LOCAL_HOSTS or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified or address.is_link_local


def _destination_rules(cidrs: str, urls: Mapping[str, str]) -> list[dict[str, Any]]:
    parts = [item.strip() for item in cidrs.split(",") if item.strip()]
    if not parts:
        return []
    ports = sorted({_url_port(url) for url in urls.values()})
    rules: list[dict[str, Any]] = []
    for cidr in parts:
        network = _city_cidr(cidr)
        rules.append(
            {
                "to": [{"ipBlock": {"cidr": str(network)}}],
                "ports": [{"protocol": "TCP", "port": port} for port in ports],
            }
        )
    return rules


def _url_port(url: str) -> int:
    parsed = urlsplit(url.strip())
    if parsed.port is not None:
        return parsed.port
    if parsed.scheme == "https":
        return 443
    if parsed.scheme == "http":
        return 80
    raise CapError("POLICY_VIOLATION", "city url")


def _city_cidr(value: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError as exc:
        raise CapError("SCHEMA_INVALID", "CITY_EGRESS_CIDRS") from exc
    too_wide = (isinstance(network, ipaddress.IPv4Network) and network.prefixlen < 16) or (
        isinstance(network, ipaddress.IPv6Network) and network.prefixlen < 48
    )
    if network.prefixlen == 0 or too_wide:
        raise CapError("POLICY_VIOLATION", "CITY_EGRESS_CIDRS")
    return network
