from __future__ import annotations

import subprocess

from mikrotik_rds_csi import node_status


def test_parse_route_get_extracts_device_and_source():
    output = "172.16.100.125 dev ens224 src 172.16.100.134 uid 0\n    cache"
    assert node_status.parse_route_get(output) == ("ens224", "172.16.100.134")


def test_collect_node_status_flags_configured_interface_mismatch(monkeypatch):
    config = node_status.NodeStatusConfig(
        node_id="ocp134.localdomain",
        target_address="172.16.100.125",
        target_port=4420,
        storage_interface="ens224.100",
        interval_seconds=15,
        tcp_timeout_seconds=2,
        namespace="mikrotik-rds-csi",
    )
    monkeypatch.setattr(
        node_status,
        "route_status",
        lambda target: {
            "ok": True,
            "interface": "ens224",
            "sourceIp": "172.16.100.134",
            "raw": "172.16.100.125 dev ens224 src 172.16.100.134",
            "error": None,
        },
    )
    monkeypatch.setattr(
        node_status,
        "interface_status",
        lambda interface: {"exists": True, "mtu": 9000, "operstate": "up", "error": None},
    )
    monkeypatch.setattr(node_status, "tcp_reachable", lambda *args: (True, None))
    monkeypatch.setattr(node_status, "nvme_tcp_sessions", lambda target: [])

    result = node_status.collect_node_status(config)

    assert result["healthy"] is False
    assert result["interfaceMatchesConfiguration"] is False
    assert "does not match configured ens224.100" in result["errors"][0]


def test_route_status_reports_command_failure(monkeypatch):
    monkeypatch.setattr(
        node_status,
        "run_command",
        lambda args: (2, "", "Network is unreachable"),
    )
    result = node_status.route_status("172.16.100.125")
    assert result["ok"] is False
    assert result["error"] == "Network is unreachable"
