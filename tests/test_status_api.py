from __future__ import annotations

from datetime import datetime, timezone
import json

from mikrotik_rds_csi.config import ControllerConfig
from mikrotik_rds_csi.status_api import StatusAPIConfig, StatusAggregator, _managed_pvs


class FakeKube:
    def __init__(self, responses):
        self.responses = responses

    def get(self, path, **kwargs):
        key = (path, tuple(sorted((kwargs.get("params") or {}).items())))
        return self.responses.get(key, self.responses.get(path, {}))


def _aggregator(fake_kube):
    config = StatusAPIConfig.from_env(static_dir="/tmp", tls_cert="/tmp/cert", tls_key="/tmp/key")
    controller = ControllerConfig(
        api_endpoint="https://rds.example",
        username="u",
        password="p",
        api_timeout_seconds=15,
        tls_verify=True,
        ca_file="",
        pool_slot="raid10",
        pool_path="/raid10",
        pool_filesystem="xfs",
        pool_required_state="clean",
        require_pool_mounted=True,
        storage_target="172.16.100.125",
        storage_port=4420,
        nqn_prefix="nqn.test",
        nsid=1,
        reserve_bytes=1024,
        file_extension=".img",
    )
    agg = StatusAggregator.__new__(StatusAggregator)
    agg.config = config
    agg.controller_config = controller
    agg.kube = fake_kube
    agg.rds = None
    return agg


def test_managed_pvs_only_returns_rds_driver():
    items = [
        {"metadata": {"name": "rds"}, "spec": {"csi": {"driver": "rds.csi.mikrotik.com"}}},
        {"metadata": {"name": "other"}, "spec": {"csi": {"driver": "other.csi"}}},
    ]
    assert [pv["metadata"]["name"] for pv in _managed_pvs(items)] == ["rds"]


def test_node_status_detects_duplicate_source_ips():
    observed = datetime.now(timezone.utc).isoformat()
    reports = []
    for node in ("ocp133.localdomain", "ocp134.localdomain"):
        reports.append(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "data": {
                    "status.json": json.dumps(
                        {
                            "node": node,
                            "observedAt": observed,
                            "route": {"sourceIp": "172.16.100.133"},
                            "healthy": True,
                            "errors": [],
                        }
                    )
                },
            }
        )
    key = (
        "/api/v1/namespaces/mikrotik-rds-csi/configmaps",
        (("labelSelector", "rds.csi.mikrotik.com/node-status=true"),),
    )
    pod_key = (
        "/api/v1/namespaces/mikrotik-rds-csi/pods",
        (("labelSelector", "app=mikrotik-rds-csi-node"),),
    )
    node_pods = {
        "items": [
            {
                "metadata": {"name": f"csi-{node}"},
                "spec": {"nodeName": node},
                "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]},
            }
            for node in ("ocp133.localdomain", "ocp134.localdomain")
        ]
    }
    csinodes = {
        "items": [
            {
                "metadata": {"name": node},
                "spec": {"drivers": [{"name": "rds.csi.mikrotik.com"}]},
            }
            for node in ("ocp133.localdomain", "ocp134.localdomain")
        ]
    }
    agg = _aggregator(
        FakeKube(
            {
                key: {"items": reports},
                pod_key: node_pods,
                "/apis/storage.k8s.io/v1/csinodes": csinodes,
            }
        )
    )

    result = agg.node_status()

    assert result["healthy"] is False
    assert result["duplicateSourceIps"] == [
        {
            "sourceIp": "172.16.100.133",
            "nodes": ["ocp133.localdomain", "ocp134.localdomain"],
        }
    ]
    assert all(item["sourceIpUnique"] is False for item in result["items"])
    assert all(item["healthy"] is False for item in result["items"])


def test_volumes_exposes_block_and_filesystem_metadata():
    pvs = {
        "items": [
            {
                "metadata": {"name": "pv-rds"},
                "spec": {
                    "capacity": {"storage": "500Gi"},
                    "volumeMode": "Filesystem",
                    "claimRef": {"namespace": "openshift-image-registry", "name": "image-registry-rds"},
                    "persistentVolumeReclaimPolicy": "Delete",
                    "csi": {
                        "driver": "rds.csi.mikrotik.com",
                        "volumeHandle": "csi-0123456789abcdef0123456789ab",
                        "volumeAttributes": {
                            "nqn": "nqn.test.csi-0123456789abcdef0123456789ab",
                            "targetAddress": "172.16.100.125",
                            "targetPort": "4420",
                        },
                    },
                },
                "status": {"phase": "Bound"},
            }
        ]
    }
    pvcs = {
        "items": [
            {
                "metadata": {"namespace": "openshift-image-registry", "name": "image-registry-rds"},
                "status": {"phase": "Bound"},
            }
        ]
    }
    pods = {
        "items": [
            {
                "metadata": {"namespace": "openshift-image-registry", "name": "image-registry-abc"},
                "spec": {
                    "nodeName": "ocp134.localdomain",
                    "volumes": [{"persistentVolumeClaim": {"claimName": "image-registry-rds"}}],
                },
            }
        ]
    }
    agg = _aggregator(
        FakeKube(
            {
                "/api/v1/persistentvolumes": pvs,
                "/api/v1/persistentvolumeclaims": pvcs,
                "/api/v1/pods": pods,
            }
        )
    )

    result = agg.volumes()

    assert result["count"] == 1
    volume = result["items"][0]
    assert volume["mode"] == "Filesystem"
    assert volume["capacity"] == "500Gi"
    assert volume["targetAddress"] == "172.16.100.125"
    assert volume["consumers"][0]["node"] == "ocp134.localdomain"


def test_cluster_status_compares_registration_to_daemonset_expected_nodes():
    ns = "mikrotik-rds-csi"
    agg = _aggregator(
        FakeKube(
            {
                f"/apis/apps/v1/namespaces/{ns}/deployments/mikrotik-rds-csi-controller": {
                    "spec": {"replicas": 1},
                    "status": {"readyReplicas": 1},
                },
                f"/apis/apps/v1/namespaces/{ns}/daemonsets/mikrotik-rds-csi-node": {
                    "status": {"desiredNumberScheduled": 3, "numberReady": 3},
                },
                (
                    f"/api/v1/namespaces/{ns}/pods",
                    (("labelSelector", "app=mikrotik-rds-csi-node"),),
                ): {
                    "items": [
                        {"metadata": {"name": f"csi-node-{i}"}, "spec": {"nodeName": f"ocp13{i}"}}
                        for i in (3, 4, 5)
                    ]
                },
                "/apis/storage.k8s.io/v1/csinodes": {
                    "items": [
                        {"metadata": {"name": f"ocp13{i}"}, "spec": {"drivers": [{"name": "rds.csi.mikrotik.com"}]}}
                        for i in (3, 4, 5)
                    ]
                    + [
                        {"metadata": {"name": "control-plane"}, "spec": {"drivers": [{"name": "other.csi"}]}}
                    ]
                },
                "/apis/storage.k8s.io/v1/storageclasses": {
                    "items": [{"metadata": {"name": "mikrotik-rds-nvme"}, "provisioner": "rds.csi.mikrotik.com"}]
                },
            }
        )
    )

    result = agg.cluster_status()

    assert result["csiRegistration"] == {"registered": 3, "totalNodes": 3, "healthy": True}


def test_node_status_does_not_add_unrelated_csinodes_to_node_table():
    observed = datetime.now(timezone.utc).isoformat()
    report = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "data": {
            "status.json": json.dumps(
                {
                    "node": "ocp133.localdomain",
                    "observedAt": observed,
                    "target": {"address": "172.16.100.125", "port": 4420},
                    "route": {"ok": True, "interface": "ens224", "sourceIp": "172.16.100.133"},
                    "interface": {"exists": True, "mtu": 9000, "operstate": "up"},
                    "targetReachable": True,
                    "activeControllers": 0,
                    "connectedNQNs": [],
                    "controllers": [],
                    "healthy": True,
                    "errors": [],
                }
            )
        },
    }
    cm_key = (
        "/api/v1/namespaces/mikrotik-rds-csi/configmaps",
        (("labelSelector", "rds.csi.mikrotik.com/node-status=true"),),
    )
    pod_key = (
        "/api/v1/namespaces/mikrotik-rds-csi/pods",
        (("labelSelector", "app=mikrotik-rds-csi-node"),),
    )
    agg = _aggregator(
        FakeKube(
            {
                cm_key: {"items": [report]},
                pod_key: {
                    "items": [
                        {
                            "metadata": {"name": "node-pod"},
                            "spec": {"nodeName": "ocp133.localdomain"},
                            "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]},
                        }
                    ]
                },
                "/apis/storage.k8s.io/v1/csinodes": {
                    "items": [
                        {"metadata": {"name": "ocp133.localdomain"}, "spec": {"drivers": [{"name": "rds.csi.mikrotik.com"}]}},
                        {"metadata": {"name": "control-plane.localdomain"}, "spec": {"drivers": []}},
                    ]
                },
            }
        )
    )

    result = agg.node_status()

    assert [item["node"] for item in result["items"]] == ["ocp133.localdomain"]
    assert result["items"][0]["csiRegistered"] is True
    assert result["healthy"] is True
