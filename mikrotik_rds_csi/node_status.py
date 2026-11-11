from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import socket
import subprocess
import time
from typing import Any

from .kube import InClusterKubeClient, KubernetesAPIError


LOG = logging.getLogger(__name__)

_ROUTE_DEV_RE = re.compile(r"\bdev\s+(\S+)")
_ROUTE_SRC_RE = re.compile(r"\bsrc\s+(\S+)")


def _parse_bool(value: str, default: bool = True) -> bool:
    if value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class NodeStatusConfig:
    node_id: str
    target_address: str
    target_port: int
    storage_interface: str
    interval_seconds: int
    tcp_timeout_seconds: float
    namespace: str

    @classmethod
    def from_env(cls) -> "NodeStatusConfig":
        target = os.getenv("RDS_NVME_TARGET_ADDRESS") or os.getenv("RDS_STORAGE_TARGET") or ""
        if not target:
            raise RuntimeError("RDS_NVME_TARGET_ADDRESS is required for node status collection")
        return cls(
            node_id=os.environ["NODE_ID"],
            target_address=target,
            target_port=int(os.getenv("RDS_NVME_TARGET_PORT") or os.getenv("RDS_STORAGE_PORT") or "4420"),
            storage_interface=os.getenv("RDS_STORAGE_INTERFACE", "").strip(),
            interval_seconds=max(5, int(os.getenv("RDS_STATUS_INTERVAL_SECONDS", "15"))),
            tcp_timeout_seconds=max(0.5, float(os.getenv("RDS_STATUS_TCP_TIMEOUT_SECONDS", "2"))),
            namespace=os.getenv("POD_NAMESPACE", "mikrotik-rds-csi"),
        )


def run_command(args: list[str]) -> tuple[int, str, str]:
    completed = subprocess.run(args, text=True, capture_output=True, check=False)
    return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


def parse_route_get(output: str) -> tuple[str | None, str | None]:
    dev = _ROUTE_DEV_RE.search(output)
    src = _ROUTE_SRC_RE.search(output)
    return (dev.group(1) if dev else None, src.group(1) if src else None)


def route_status(target: str) -> dict[str, Any]:
    rc, stdout, stderr = run_command(["ip", "route", "get", target])
    if rc != 0:
        return {
            "ok": False,
            "interface": None,
            "sourceIp": None,
            "raw": stdout,
            "error": stderr or f"ip route get exited with {rc}",
        }
    interface, source_ip = parse_route_get(stdout)
    return {
        "ok": bool(interface and source_ip),
        "interface": interface,
        "sourceIp": source_ip,
        "raw": stdout,
        "error": None if interface and source_ip else "route did not include dev/src",
    }


def interface_status(interface: str | None) -> dict[str, Any]:
    if not interface:
        return {"exists": False, "mtu": None, "operstate": None, "error": "no route interface"}
    base = Path("/sys/class/net") / interface
    if not base.exists():
        return {
            "exists": False,
            "mtu": None,
            "operstate": None,
            "error": f"interface {interface} not found in sysfs",
        }
    try:
        mtu = int((base / "mtu").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        mtu = None
    try:
        operstate = (base / "operstate").read_text(encoding="utf-8").strip()
    except OSError:
        operstate = None
    return {"exists": True, "mtu": mtu, "operstate": operstate, "error": None}


def tcp_reachable(target: str, port: int, source_ip: str | None, timeout: float) -> tuple[bool, str | None]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        if source_ip:
            sock.bind((source_ip, 0))
        sock.connect((target, port))
        return True, None
    except OSError as exc:
        return False, str(exc)
    finally:
        sock.close()


def _controller_paths() -> list[Path]:
    root = Path("/sys/class/nvme-subsystem")
    if not root.exists():
        return []
    paths: list[Path] = []
    for subsystem in sorted(root.glob("nvme-subsys*")):
        for item in subsystem.iterdir():
            if re.fullmatch(r"nvme\d+", item.name):
                paths.append(item)
    return paths


def nvme_tcp_sessions(target: str) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for controller in _controller_paths():
        subsystem = controller.parent
        try:
            nqn = (subsystem / "subsysnqn").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        try:
            transport = (controller / "transport").read_text(encoding="utf-8").strip()
        except OSError:
            transport = ""
        if transport != "tcp":
            continue
        try:
            address = (controller / "address").read_text(encoding="utf-8").strip()
        except OSError:
            address = ""
        if target and f"traddr={target}" not in address:
            continue
        try:
            state = (controller / "state").read_text(encoding="utf-8").strip()
        except OSError:
            state = "unknown"
        key = (controller.name, nqn)
        if key in seen:
            continue
        seen.add(key)
        sessions.append(
            {
                "controller": controller.name,
                "nqn": nqn,
                "state": state,
                "address": address,
            }
        )
    return sessions


def collect_node_status(config: NodeStatusConfig) -> dict[str, Any]:
    route = route_status(config.target_address)
    interface = interface_status(route.get("interface"))
    source_ip = route.get("sourceIp")
    reachable, tcp_error = tcp_reachable(
        config.target_address,
        config.target_port,
        source_ip,
        config.tcp_timeout_seconds,
    )
    sessions = nvme_tcp_sessions(config.target_address)
    expected_iface = config.storage_interface or None
    interface_matches = expected_iface is None or route.get("interface") == expected_iface
    errors = [
        err
        for err in (
            route.get("error"),
            interface.get("error"),
            tcp_error,
            None if interface_matches else f"route interface {route.get('interface')} does not match configured {expected_iface}",
        )
        if err
    ]
    return {
        "apiVersion": "rds.csi.mikrotik.com/v1alpha1",
        "node": config.node_id,
        "observedAt": datetime.now(timezone.utc).isoformat(),
        "target": {"address": config.target_address, "port": config.target_port},
        "route": route,
        "interface": interface,
        "configuredInterface": expected_iface,
        "interfaceMatchesConfiguration": interface_matches,
        "targetReachable": reachable,
        "activeControllers": len(sessions),
        "connectedNQNs": sorted({item["nqn"] for item in sessions}),
        "controllers": sessions,
        "healthy": bool(route.get("ok") and interface.get("exists") and reachable and interface_matches),
        "errors": errors,
    }


def _configmap_name(node_id: str) -> str:
    cleaned = re.sub(r"[^a-z0-9.-]", "-", node_id.lower()).strip("-.")
    return f"mikrotik-rds-node-status-{cleaned}"[:253].rstrip("-.")


def publish_status(kube: InClusterKubeClient, namespace: str, status: dict[str, Any]) -> None:
    name = _configmap_name(str(status["node"]))
    path = f"/api/v1/namespaces/{namespace}/configmaps/{name}"
    payload = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": "mikrotik-rds-csi",
                "app.kubernetes.io/component": "node-status",
                "rds.csi.mikrotik.com/node-status": "true",
            },
        },
        "data": {"status.json": json.dumps(status, sort_keys=True)},
    }
    try:
        existing = kube.get(path, expected=(200, 404))
    except KubernetesAPIError as exc:
        # request() cannot currently return 404 data; fall back to direct create path below.
        if "returned 404" not in str(exc):
            raise
        existing = None

    if existing and existing.get("metadata", {}).get("resourceVersion"):
        payload["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
        kube.put(path, json=payload, expected=(200,))
    else:
        kube.post(f"/api/v1/namespaces/{namespace}/configmaps", json=payload, expected=(201,))


def run_node_status_reporter(config: NodeStatusConfig | None = None) -> None:
    config = config or NodeStatusConfig.from_env()
    kube = InClusterKubeClient()
    LOG.info(
        "node status reporter started for %s -> %s:%s every %ss",
        config.node_id,
        config.target_address,
        config.target_port,
        config.interval_seconds,
    )
    last_connectivity_error: str | None = None
    last_connectivity_error_at: str | None = None
    while True:
        try:
            status = collect_node_status(config)
            if status["errors"]:
                last_connectivity_error = "; ".join(str(item) for item in status["errors"])
                last_connectivity_error_at = str(status["observedAt"])
            status["lastConnectivityError"] = last_connectivity_error
            status["lastConnectivityErrorAt"] = last_connectivity_error_at
            publish_status(kube, config.namespace, status)
            if status["healthy"]:
                LOG.debug("published healthy node status for %s", config.node_id)
            else:
                LOG.warning("published unhealthy node status for %s: %s", config.node_id, status["errors"])
        except Exception:
            LOG.exception("failed to collect or publish node status for %s", config.node_id)
        time.sleep(config.interval_seconds)
