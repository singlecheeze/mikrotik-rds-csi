from __future__ import annotations

import subprocess

from mikrotik_rds_csi import nvme


def test_is_mountpoint_checks_exact_path(monkeypatch):
    calls = []

    def fake_run(args, *, check=True):
        calls.append((args, check))
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="")

    monkeypatch.setattr(nvme, "_run", fake_run)

    path = "/var/lib/kubelet/plugins/kubernetes.io/csi/volumeDevices/publish/pvc-123/pod-456"
    assert nvme.is_mountpoint(path) is False
    assert calls == [(["findmnt", "-rn", "--mountpoint", path], False)]


def test_mounted_source_checks_exact_mountpoint(monkeypatch):
    calls = []

    def fake_run(args, *, check=True):
        calls.append((args, check))
        return subprocess.CompletedProcess(args, 0, stdout="/dev/nvme1n1\n", stderr="")

    monkeypatch.setattr(nvme, "_run", fake_run)

    path = "/var/lib/kubelet/plugins/kubernetes.io/csi/volumeDevices/publish/pvc-123/pod-456"
    assert nvme.mounted_source(path) == "/dev/nvme1n1"
    assert calls == [(["findmnt", "-rn", "-o", "SOURCE", "--mountpoint", path], False)]
