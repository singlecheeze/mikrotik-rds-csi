from __future__ import annotations

import subprocess

import pytest

from mikrotik_rds_csi import nvme


def test_existing_xfs_is_reused_without_format(monkeypatch):
    calls = []

    def fake_run(args, *, check=True):
        calls.append((args, check))
        return subprocess.CompletedProcess(
            args,
            0,
            stdout="DEVNAME=/dev/nvme1n1\nTYPE=xfs\nUUID=abc\n",
            stderr="",
        )

    monkeypatch.setattr(nvme, "_run", fake_run)

    assert nvme.ensure_filesystem("/dev/nvme1n1", "xfs") is False
    assert calls == [(["blkid", "-p", "-o", "export", "/dev/nvme1n1"], False)]


def test_blank_device_is_formatted_once_without_force(monkeypatch):
    calls = []
    responses = [
        subprocess.CompletedProcess([], 2, stdout="", stderr=""),
        subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        subprocess.CompletedProcess([], 0, stdout="TYPE=xfs\nUUID=abc\n", stderr=""),
    ]

    def fake_run(args, *, check=True):
        calls.append((args, check))
        response = responses.pop(0)
        return subprocess.CompletedProcess(args, response.returncode, response.stdout, response.stderr)

    monkeypatch.setattr(nvme, "_run", fake_run)

    assert nvme.ensure_filesystem("/dev/nvme1n1", "xfs") is True
    assert calls[0] == (["blkid", "-p", "-o", "export", "/dev/nvme1n1"], False)
    assert calls[1] == (["mkfs.xfs", "-q", "/dev/nvme1n1"], True)
    assert "-f" not in calls[1][0]
    assert calls[2] == (["blkid", "-p", "-o", "export", "/dev/nvme1n1"], False)


def test_existing_non_xfs_signature_is_never_formatted(monkeypatch):
    calls = []

    def fake_run(args, *, check=True):
        calls.append((args, check))
        return subprocess.CompletedProcess(args, 0, stdout="TYPE=ext4\nUUID=abc\n", stderr="")

    monkeypatch.setattr(nvme, "_run", fake_run)

    with pytest.raises(nvme.NVMeError, match="refusing to format"):
        nvme.ensure_filesystem("/dev/nvme1n1", "xfs")

    assert not any(args and args[0] == "mkfs.xfs" for args, _ in calls)


def test_unknown_existing_signature_is_never_formatted(monkeypatch):
    calls = []

    def fake_run(args, *, check=True):
        calls.append((args, check))
        return subprocess.CompletedProcess(args, 0, stdout="PTTYPE=gpt\nPTUUID=abc\n", stderr="")

    monkeypatch.setattr(nvme, "_run", fake_run)

    with pytest.raises(nvme.NVMeError, match="existing block signature"):
        nvme.ensure_filesystem("/dev/nvme1n1", "xfs")

    assert not any(args and args[0] == "mkfs.xfs" for args, _ in calls)


def test_mount_publish_reuses_xfs_and_passes_mount_flags(monkeypatch, tmp_path):
    target = tmp_path / "target"
    calls = []
    ensured = []

    monkeypatch.setattr(nvme, "is_mountpoint", lambda path: False)
    monkeypatch.setattr(
        nvme,
        "ensure_filesystem",
        lambda device, fs_type: ensured.append((device, fs_type)) or False,
    )

    def fake_run(args, *, check=True):
        calls.append((args, check))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(nvme, "_run", fake_run)

    nvme.mount_publish(
        "/dev/nvme1n1",
        str(target),
        fs_type="xfs",
        mount_flags=["noatime"],
        readonly=True,
    )

    assert target.is_dir()
    assert ensured == [("/dev/nvme1n1", "xfs")]
    assert calls == [
        (["mount", "-t", "xfs", "-o", "noatime,ro", "/dev/nvme1n1", str(target)], True)
    ]
