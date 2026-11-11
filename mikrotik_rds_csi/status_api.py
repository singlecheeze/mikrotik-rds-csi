from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import ssl
from typing import Any
from urllib.parse import urlparse

from .config import ControllerConfig
from .kube import InClusterKubeClient, KubernetesAPIError
from .routeros import RouterOSClient, RouterOSError


LOG = logging.getLogger(__name__)
DRIVER_NAME = "rds.csi.mikrotik.com"
MANAGED_ID_RE = re.compile(r"^csi-[0-9a-f]{28}$")


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _condition_ready(obj: dict[str, Any]) -> bool:
    conditions = obj.get("status", {}).get("conditions", []) or []
    for condition in conditions:
        if condition.get("type") in {"Available", "Ready"} and condition.get("status") == "True":
            return True
    return False


def _daemonset_ready(obj: dict[str, Any]) -> tuple[int, int]:
    status = obj.get("status", {})
    desired = _int(status.get("desiredNumberScheduled"))
    ready = _int(status.get("numberReady"))
    return ready, desired


def _deployment_ready(obj: dict[str, Any]) -> tuple[int, int]:
    status = obj.get("status", {})
    desired = _int(obj.get("spec", {}).get("replicas"), 1)
    ready = _int(status.get("readyReplicas"))
    return ready, desired


def _parse_rfc3339(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _event_time(event: dict[str, Any]) -> datetime:
    for field in ("eventTime", "lastTimestamp", "firstTimestamp"):
        parsed = _parse_rfc3339(event.get(field))
        if parsed:
            return parsed
    parsed = _parse_rfc3339(event.get("metadata", {}).get("creationTimestamp"))
    return parsed or datetime.fromtimestamp(0, timezone.utc)


def _managed_pvs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        pv
        for pv in items
        if pv.get("spec", {}).get("csi", {}).get("driver") == DRIVER_NAME
    ]


def _format_quantity_bytes(value: str | None) -> int | None:
    if not value:
        return None
    units = {
        "Ki": 1024,
        "Mi": 1024**2,
        "Gi": 1024**3,
        "Ti": 1024**4,
        "Pi": 1024**5,
        "K": 1000,
        "M": 1000**2,
        "G": 1000**3,
        "T": 1000**4,
        "P": 1000**5,
    }
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([A-Za-z]+)?", value)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2) or ""
    return int(number * units.get(unit, 1))


@dataclass(frozen=True)
class StatusAPIConfig:
    namespace: str
    storage_class: str
    static_dir: Path
    listen_address: str
    port: int
    tls_cert: Path
    tls_key: Path
    max_event_age_minutes: int

    @classmethod
    def from_env(
        cls,
        *,
        static_dir: str | None = None,
        listen_address: str | None = None,
        port: int | None = None,
        tls_cert: str | None = None,
        tls_key: str | None = None,
    ) -> "StatusAPIConfig":
        return cls(
            namespace=os.getenv("POD_NAMESPACE", "mikrotik-rds-csi"),
            storage_class=os.getenv("RDS_STORAGE_CLASS", "mikrotik-rds-nvme"),
            static_dir=Path(static_dir or os.getenv("RDS_CONSOLE_STATIC_DIR", "/opt/console-plugin")),
            listen_address=listen_address or os.getenv("RDS_CONSOLE_LISTEN_ADDRESS", "0.0.0.0"),
            port=port or int(os.getenv("RDS_CONSOLE_PORT", "9443")),
            tls_cert=Path(tls_cert or os.getenv("RDS_CONSOLE_TLS_CERT", "/var/serving-cert/tls.crt")),
            tls_key=Path(tls_key or os.getenv("RDS_CONSOLE_TLS_KEY", "/var/serving-cert/tls.key")),
            max_event_age_minutes=max(5, int(os.getenv("RDS_STATUS_EVENT_AGE_MINUTES", "60"))),
        )


class StatusAggregator:
    def __init__(self, config: StatusAPIConfig, controller_config: ControllerConfig) -> None:
        self.config = config
        self.controller_config = controller_config
        self.kube = InClusterKubeClient()
        self.rds = RouterOSClient(
            controller_config.api_endpoint,
            controller_config.username,
            controller_config.password,
            verify=controller_config.requests_verify,
            timeout=controller_config.api_timeout_seconds,
        )

    def authorize(self, user_token: str) -> bool:
        # Node and PV visibility are intentionally required before exposing storage-network
        # diagnostics or RouterOS pool information through the console proxy.
        return self.kube.self_subject_access_review(
            user_token, verb="list", resource="nodes"
        ) and self.kube.self_subject_access_review(
            user_token, verb="list", resource="persistentvolumes"
        )

    def rds_status(self) -> dict[str, Any]:
        try:
            system = self.rds.get_system_resource()
            disks = self.rds.list_disks()
            pool = next(
                (d for d in disks if d.get("slot") == self.controller_config.pool_slot),
                None,
            )
            if not pool:
                raise RouterOSError(
                    f"pool slot {self.controller_config.pool_slot!r} was not found"
                )
            free_bytes = _int(pool.get("free"))
            total_bytes = _int(pool.get("size"))
            usable_bytes = max(0, free_bytes - self.controller_config.reserve_bytes)
            managed = [d for d in disks if MANAGED_ID_RE.fullmatch(str(d.get("slot", "")))]
            exports = [d for d in managed if _truthy(d.get("nvme-tcp-export"))]
            state = str(pool.get("state", ""))
            mounted = _truthy(pool.get("mounted"))
            filesystem = str(pool.get("fs", ""))
            healthy = (
                mounted
                and (not self.controller_config.pool_required_state or state == self.controller_config.pool_required_state)
                and (not self.controller_config.pool_filesystem or filesystem == self.controller_config.pool_filesystem)
            )
            return {
                "reachable": True,
                "healthy": healthy,
                "routerOsVersion": system.get("version"),
                "boardName": system.get("board-name"),
                "uptime": system.get("uptime"),
                "pool": {
                    "slot": self.controller_config.pool_slot,
                    "path": self.controller_config.pool_path,
                    "state": state,
                    "mounted": mounted,
                    "filesystem": filesystem,
                    "freeBytes": free_bytes,
                    "totalBytes": total_bytes,
                    "usableBytes": usable_bytes,
                    "reserveBytes": self.controller_config.reserve_bytes,
                },
                "managedVolumes": len(managed),
                "activeExports": len(exports),
                "target": {
                    "address": self.controller_config.storage_target,
                    "port": self.controller_config.storage_port,
                },
                "error": None,
            }
        except Exception as exc:
            return {
                "reachable": False,
                "healthy": False,
                "routerOsVersion": None,
                "pool": None,
                "managedVolumes": None,
                "activeExports": None,
                "target": {
                    "address": self.controller_config.storage_target,
                    "port": self.controller_config.storage_port,
                },
                "error": str(exc),
            }

    def cluster_status(self) -> dict[str, Any]:
        ns = self.config.namespace
        controller = self.kube.get(
            f"/apis/apps/v1/namespaces/{ns}/deployments/mikrotik-rds-csi-controller"
        )
        nodes_ds = self.kube.get(
            f"/apis/apps/v1/namespaces/{ns}/daemonsets/mikrotik-rds-csi-node"
        )
        csinodes = self.kube.get("/apis/storage.k8s.io/v1/csinodes")
        storageclasses = self.kube.get("/apis/storage.k8s.io/v1/storageclasses")
        node_pods = self.kube.get(
            f"/api/v1/namespaces/{ns}/pods",
            params={"labelSelector": "app=mikrotik-rds-csi-node"},
        )
        controller_ready, controller_desired = _deployment_ready(controller)
        node_ready, node_desired = _daemonset_ready(nodes_ds)

        # Count registration only on nodes where the CSI DaemonSet currently has a
        # pod. The DaemonSet's desired count remains the denominator, so a missing
        # pod still makes registration unhealthy without unrelated CSINodes (for
        # example an untargeted control-plane node) creating a false alarm.
        pod_nodes = {
            str(pod.get("spec", {}).get("nodeName", ""))
            for pod in node_pods.get("items", [])
            if pod.get("spec", {}).get("nodeName")
        }
        registered_nodes: set[str] = set()
        for csinode in csinodes.get("items", []):
            node_name = str(csinode.get("metadata", {}).get("name", ""))
            if node_name not in pod_nodes:
                continue
            drivers = csinode.get("spec", {}).get("drivers", []) or []
            if any(d.get("name") == DRIVER_NAME for d in drivers):
                registered_nodes.add(node_name)
        registered = len(registered_nodes)
        expected_registrations = node_desired
        sc_exists = any(
            item.get("metadata", {}).get("name") == self.config.storage_class
            and item.get("provisioner") == DRIVER_NAME
            for item in storageclasses.get("items", [])
        )
        return {
            "controller": {
                "ready": controller_ready,
                "desired": controller_desired,
                "healthy": controller_desired > 0 and controller_ready == controller_desired,
            },
            "nodePlugin": {
                "ready": node_ready,
                "desired": node_desired,
                "healthy": node_desired > 0 and node_ready == node_desired,
            },
            "csiRegistration": {
                "registered": registered,
                "totalNodes": expected_registrations,
                "healthy": expected_registrations > 0 and registered == expected_registrations,
            },
            "storageClass": {
                "name": self.config.storage_class,
                "exists": sc_exists,
                "healthy": sc_exists,
            },
        }

    def node_status(self) -> dict[str, Any]:
        ns = self.config.namespace
        cms = self.kube.get(
            f"/api/v1/namespaces/{ns}/configmaps",
            params={"labelSelector": "rds.csi.mikrotik.com/node-status=true"},
        )
        node_pods = self.kube.get(
            f"/api/v1/namespaces/{ns}/pods",
            params={"labelSelector": "app=mikrotik-rds-csi-node"},
        )
        csinodes = self.kube.get("/apis/storage.k8s.io/v1/csinodes")

        pod_by_node: dict[str, dict[str, Any]] = {}
        for pod in node_pods.get("items", []):
            node_name = str(pod.get("spec", {}).get("nodeName", ""))
            if not node_name:
                continue
            ready = any(
                c.get("type") == "Ready" and c.get("status") == "True"
                for c in pod.get("status", {}).get("conditions", []) or []
            )
            pod_by_node[node_name] = {
                "name": pod.get("metadata", {}).get("name"),
                "ready": ready,
                "phase": pod.get("status", {}).get("phase"),
            }

        registered_by_node: dict[str, bool] = {}
        for csinode in csinodes.get("items", []):
            node_name = str(csinode.get("metadata", {}).get("name", ""))
            drivers = csinode.get("spec", {}).get("drivers", []) or []
            registered_by_node[node_name] = any(d.get("name") == DRIVER_NAME for d in drivers)

        reports_by_node: dict[str, dict[str, Any]] = {}
        for item in cms.get("items", []):
            raw = item.get("data", {}).get("status.json")
            if not raw:
                continue
            try:
                report = json.loads(raw)
            except json.JSONDecodeError:
                continue
            node_name = str(report.get("node", ""))
            if node_name:
                reports_by_node[node_name] = report

        # A CSINode object exists for every Kubernetes node, and the optional
        # diagnostic DaemonSet can be scheduled more broadly than the CSI node
        # DaemonSet. Show actual CSI node pods plus report-only nodes that still
        # have this driver registered; ignore unrelated diagnostic reports.
        expected_nodes = sorted(
            set(pod_by_node)
            | {node for node in reports_by_node if registered_by_node.get(node)}
        )
        reports: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)
        for node_name in expected_nodes:
            report = reports_by_node.get(node_name)
            if report is None:
                report = {
                    "node": node_name,
                    "observedAt": None,
                    "target": {
                        "address": self.controller_config.storage_target,
                        "port": self.controller_config.storage_port,
                    },
                    "route": {"ok": False, "interface": None, "sourceIp": None, "raw": None, "error": "no node status report"},
                    "interface": {"exists": False, "mtu": None, "operstate": None, "error": "no node status report"},
                    "configuredInterface": None,
                    "interfaceMatchesConfiguration": False,
                    "targetReachable": False,
                    "activeControllers": 0,
                    "connectedNQNs": [],
                    "controllers": [],
                    "healthy": False,
                    "errors": ["no node status report"],
                }
            report["csiPod"] = pod_by_node.get(node_name)
            report["csiPodReady"] = bool((pod_by_node.get(node_name) or {}).get("ready"))
            report["csiRegistered"] = bool(registered_by_node.get(node_name))
            observed = _parse_rfc3339(report.get("observedAt"))
            age = (now - observed).total_seconds() if observed else None
            report["ageSeconds"] = age
            report["stale"] = age is None or age > max(45, int(os.getenv("RDS_STATUS_INTERVAL_SECONDS", "15")) * 3)
            if not report["csiPodReady"]:
                report.setdefault("errors", []).append("CSI node pod is not Ready")
                report["healthy"] = False
            if not report["csiRegistered"]:
                report.setdefault("errors", []).append("CSI driver is not registered in CSINode")
                report["healthy"] = False
            reports.append(report)

        by_source: dict[str, list[str]] = {}
        for report in reports:
            source = report.get("route", {}).get("sourceIp")
            if source:
                by_source.setdefault(source, []).append(str(report.get("node")))

        duplicates = [
            {"sourceIp": source, "nodes": sorted(nodes)}
            for source, nodes in by_source.items()
            if len(nodes) > 1
        ]
        duplicate_ips = {item["sourceIp"] for item in duplicates}
        for report in reports:
            source = report.get("route", {}).get("sourceIp")
            report["sourceIpUnique"] = source is not None and source not in duplicate_ips
            if source in duplicate_ips:
                report.setdefault("errors", []).append(
                    f"duplicate source IP {source} is reported by multiple nodes"
                )
                report["healthy"] = False
            if report.get("stale"):
                report.setdefault("errors", []).append("node status report is stale")
                report["healthy"] = False

        reports.sort(key=lambda x: str(x.get("node", "")))
        return {
            "items": reports,
            "duplicateSourceIps": duplicates,
            "healthy": bool(reports) and not duplicates and all(r.get("healthy") for r in reports),
        }

    def volumes(self) -> dict[str, Any]:
        pvs = self.kube.get("/api/v1/persistentvolumes").get("items", [])
        pods = self.kube.get("/api/v1/pods").get("items", [])
        claims = self.kube.get("/api/v1/persistentvolumeclaims").get("items", [])
        managed = _managed_pvs(pvs)

        pvc_index: dict[tuple[str, str], dict[str, Any]] = {}
        for claim in claims:
            meta = claim.get("metadata", {})
            pvc_index[(meta.get("namespace", ""), meta.get("name", ""))] = claim

        consumers: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for pod in pods:
            meta = pod.get("metadata", {})
            namespace = meta.get("namespace", "")
            for volume in pod.get("spec", {}).get("volumes", []) or []:
                pvc = volume.get("persistentVolumeClaim")
                if not pvc:
                    continue
                key = (namespace, pvc.get("claimName", ""))
                labels = meta.get("labels", {}) or {}
                vm_name = labels.get("vm.kubevirt.io/name") or labels.get("kubevirt.io/domain")
                consumers.setdefault(key, []).append(
                    {
                        "kind": "VirtualMachine" if vm_name else "Pod",
                        "namespace": namespace,
                        "name": vm_name or meta.get("name"),
                        "pod": meta.get("name"),
                        "node": pod.get("spec", {}).get("nodeName"),
                    }
                )

        rows: list[dict[str, Any]] = []
        for pv in managed:
            meta = pv.get("metadata", {})
            spec = pv.get("spec", {})
            csi = spec.get("csi", {})
            claim_ref = spec.get("claimRef", {}) or {}
            namespace = claim_ref.get("namespace", "")
            claim_name = claim_ref.get("name", "")
            claim = pvc_index.get((namespace, claim_name), {})
            mode = spec.get("volumeMode") or claim.get("spec", {}).get("volumeMode") or "Filesystem"
            attrs = csi.get("volumeAttributes", {}) or {}
            capacity = spec.get("capacity", {}).get("storage")
            rows.append(
                {
                    "pv": meta.get("name"),
                    "namespace": namespace,
                    "pvc": claim_name,
                    "mode": mode,
                    "capacity": capacity,
                    "capacityBytes": _format_quantity_bytes(capacity),
                    "volumeHandle": csi.get("volumeHandle"),
                    "nqn": attrs.get("nqn"),
                    "targetAddress": attrs.get("targetAddress") or attrs.get("target"),
                    "targetPort": attrs.get("targetPort") or attrs.get("port"),
                    "phase": claim.get("status", {}).get("phase") or pv.get("status", {}).get("phase"),
                    "consumers": consumers.get((namespace, claim_name), []),
                    "reclaimPolicy": spec.get("persistentVolumeReclaimPolicy"),
                }
            )
        rows.sort(key=lambda x: (x.get("namespace") or "", x.get("pvc") or x.get("pv") or ""))
        return {"items": rows, "count": len(rows)}

    def events(self) -> dict[str, Any]:
        events = self.kube.get("/api/v1/events").get("items", [])
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=self.config.max_event_age_minutes)
        keywords = ("rds.csi.mikrotik.com", "nvme", "mountvolume", "mikrotik-rds", "registry-storage")
        result: list[dict[str, Any]] = []
        for event in events:
            if event.get("type") != "Warning":
                continue
            when = _event_time(event)
            if when < cutoff:
                continue
            text = " ".join(
                str(v or "")
                for v in (
                    event.get("reason"),
                    event.get("message"),
                    event.get("involvedObject", {}).get("name"),
                )
            ).lower()
            if not any(keyword in text for keyword in keywords):
                continue
            involved = event.get("involvedObject", {})
            result.append(
                {
                    "time": when.isoformat(),
                    "reason": event.get("reason"),
                    "message": event.get("message"),
                    "count": event.get("count", 1),
                    "kind": involved.get("kind"),
                    "namespace": involved.get("namespace"),
                    "name": involved.get("name"),
                    "source": event.get("source", {}).get("component") or event.get("reportingComponent"),
                }
            )
        result.sort(key=lambda x: x["time"], reverse=True)
        return {"items": result[:50], "count": len(result)}

    def overview(self) -> dict[str, Any]:
        rds = self.rds_status()
        cluster = self.cluster_status()
        nodes = self.node_status()
        volumes = self.volumes()
        events = self.events()
        healthy = bool(
            rds.get("healthy")
            and cluster["controller"]["healthy"]
            and cluster["nodePlugin"]["healthy"]
            and cluster["csiRegistration"]["healthy"]
            and cluster["storageClass"]["healthy"]
            and nodes.get("healthy")
        )
        return {
            "apiVersion": "rds.csi.mikrotik.com/v1alpha1",
            "observedAt": datetime.now(timezone.utc).isoformat(),
            "healthy": healthy,
            "cluster": cluster,
            "rds": rds,
            "nodes": nodes,
            "volumes": volumes,
            "events": events,
        }


class StatusRequestHandler(BaseHTTPRequestHandler):
    server_version = "MikroTikRDSConsole/0.4"

    @property
    def aggregator(self) -> StatusAggregator:
        return self.server.aggregator  # type: ignore[attr-defined]

    @property
    def config(self) -> StatusAPIConfig:
        return self.server.status_config  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _user_token(self) -> str | None:
        header = self.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            token = header.split(" ", 1)[1].strip()
            return token or None
        return None

    def _authorize(self) -> bool:
        token = self._user_token()
        if not token:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "OpenShift user token is required"})
            return False
        try:
            allowed = self.aggregator.authorize(token)
        except Exception as exc:
            LOG.warning("authorization check failed: %s", exc)
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "authorization check failed"})
            return False
        if not allowed:
            self._json(
                HTTPStatus.FORBIDDEN,
                {"error": "user must be allowed to list Nodes and PersistentVolumes"},
            )
            return False
        return True

    def _serve_static(self, request_path: str) -> None:
        relative = request_path.lstrip("/") or "plugin-manifest.json"
        if ".." in Path(relative).parts:
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        path = (self.config.static_dir / relative).resolve()
        try:
            path.relative_to(self.config.static_dir.resolve())
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        content_type, _ = mimetypes.guess_type(path.name)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Cache-Control", "no-cache" if path.name == "plugin-manifest.json" else "public, max-age=31536000, immutable")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/healthz":
            self._json(HTTPStatus.OK, {"status": "ok"})
            return
        if not path.startswith("/api/v1/"):
            self._serve_static(path)
            return
        if not self._authorize():
            return

        try:
            if path == "/api/v1/health":
                self._json(HTTPStatus.OK, {"status": "ok", "driver": DRIVER_NAME})
            elif path == "/api/v1/overview":
                self._json(HTTPStatus.OK, self.aggregator.overview())
            elif path == "/api/v1/rds":
                self._json(HTTPStatus.OK, self.aggregator.rds_status())
            elif path == "/api/v1/nodes":
                self._json(HTTPStatus.OK, self.aggregator.node_status())
            elif path == "/api/v1/volumes":
                self._json(HTTPStatus.OK, self.aggregator.volumes())
            elif path == "/api/v1/events":
                self._json(HTTPStatus.OK, self.aggregator.events())
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})
        except (KubernetesAPIError, RouterOSError) as exc:
            LOG.warning("status request failed: %s", exc)
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
        except Exception as exc:
            LOG.exception("unexpected status API error")
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})


class StatusHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


def serve_status_api(config: StatusAPIConfig | None = None) -> None:
    config = config or StatusAPIConfig.from_env()
    controller_config = ControllerConfig.from_env()
    aggregator = StatusAggregator(config, controller_config)
    server = StatusHTTPServer((config.listen_address, config.port), StatusRequestHandler)
    server.aggregator = aggregator  # type: ignore[attr-defined]
    server.status_config = config  # type: ignore[attr-defined]

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(config.tls_cert), keyfile=str(config.tls_key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    LOG.info(
        "MikroTik RDS console/status HTTPS service listening on %s:%s; static assets %s",
        config.listen_address,
        config.port,
        config.static_dir,
    )
    server.serve_forever()
