from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time


class NVMeError(RuntimeError):
    pass


_NAMESPACE_RE = re.compile(r"^nvme\d+(?:c\d+)?n\d+$")


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, check=check, text=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        detail = stderr or stdout or f"exit code {exc.returncode}"
        raise NVMeError(f"command {' '.join(args)!r} failed: {detail}") from exc


def load_nvme_module(module: str = "nvme_tcp") -> None:
    _run(["modprobe", module])


def _subsystem_for_nqn(nqn: str) -> Path | None:
    root = Path("/sys/class/nvme-subsystem")
    if not root.exists():
        return None
    for subsystem in root.iterdir():
        try:
            value = (subsystem / "subsysnqn").read_text().strip()
        except OSError:
            continue
        if value == nqn:
            return subsystem
    return None


def nqn_for_volume_id(volume_id: str) -> str | None:
    """Find a connected CSI NQN by its deterministic volume-id suffix.

    This is used as a recovery path if the node state file is unavailable.
    It intentionally does not assume a globally configured NQN prefix.
    """
    root = Path("/sys/class/nvme-subsystem")
    if not root.exists():
        return None
    suffix = f".{volume_id}"
    matches: list[str] = []
    for subsystem in root.iterdir():
        try:
            value = (subsystem / "subsysnqn").read_text().strip()
        except OSError:
            continue
        if value.endswith(suffix):
            matches.append(value)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise NVMeError(f"multiple connected NQNs match volume {volume_id}: {matches}")
    return None


def device_for_nqn(nqn: str, nsid: int = 1) -> str | None:
    subsystem = _subsystem_for_nqn(nqn)
    if subsystem is None:
        return None

    candidates: list[str] = []
    for child in subsystem.iterdir():
        name = child.name
        if not _NAMESPACE_RE.match(name):
            continue
        nsid_path = Path("/sys/class/block") / name / "nsid"
        try:
            current_nsid = int(nsid_path.read_text().strip())
        except (OSError, ValueError):
            continue
        if current_nsid == nsid and Path("/dev", name).exists():
            candidates.append(name)

    if not candidates:
        return None

    # Prefer the multipath namespace name (nvmeXnY) over a controller-specific
    # path such as nvmeXcYnZ when both are present.
    candidates.sort(key=lambda value: ("c" in value, value))
    return f"/dev/{candidates[0]}"


def route_to(target: str) -> tuple[str, str]:
    result = _run(["ip", "route", "get", target])
    tokens = result.stdout.strip().split()
    try:
        dev = tokens[tokens.index("dev") + 1]
    except (ValueError, IndexError) as exc:
        raise NVMeError(f"unable to parse route interface to {target}: {result.stdout.strip()}") from exc

    src = ""
    try:
        src = tokens[tokens.index("src") + 1]
    except (ValueError, IndexError):
        pass
    return dev, src


def connect(
    target: str,
    port: int,
    nqn: str,
    nsid: int = 1,
    timeout_seconds: int = 20,
    expected_interface: str | None = None,
    use_route_source_address: bool = True,
    reconnect_delay_seconds: int = 10,
    ctrl_loss_tmo_seconds: int = 600,
    nvme_module: str = "nvme_tcp",
) -> str:
    existing = device_for_nqn(nqn, nsid)
    if existing:
        return existing

    load_nvme_module(nvme_module)

    route_dev = ""
    route_src = ""
    if expected_interface or use_route_source_address:
        route_dev, route_src = route_to(target)
        if expected_interface and route_dev != expected_interface:
            raise NVMeError(
                f"route to {target} uses {route_dev}, expected {expected_interface}; "
                "refusing NVMe connect"
            )
        if use_route_source_address and not route_src:
            raise NVMeError(
                f"route to {target} did not report a source address; "
                "set RDS_USE_ROUTE_SOURCE_ADDRESS=false to omit --host-traddr"
            )

    args = [
        "nvme",
        "connect",
        "-t",
        "tcp",
        "-a",
        target,
        "-s",
        str(port),
        "-n",
        nqn,
        "--reconnect-delay",
        str(reconnect_delay_seconds),
        "--ctrl-loss-tmo",
        str(ctrl_loss_tmo_seconds),
    ]
    if use_route_source_address:
        args.extend(["--host-traddr", route_src])

    try:
        _run(args)
    except NVMeError:
        # Treat a racing/already-connected result as success if the namespace
        # appears while another kubelet operation was connecting it.
        existing = device_for_nqn(nqn, nsid)
        if existing:
            return existing
        raise

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        device = device_for_nqn(nqn, nsid)
        if device:
            return device
        time.sleep(0.25)

    raise NVMeError(f"NVMe namespace for {nqn} did not appear within {timeout_seconds}s")


def disconnect(nqn: str) -> None:
    if _subsystem_for_nqn(nqn) is None:
        return
    _run(["nvme", "disconnect", "-n", nqn])


def is_mountpoint(path: str) -> bool:
    result = _run(["findmnt", "-rn", "--target", path], check=False)
    return result.returncode == 0


def mounted_source(path: str) -> str | None:
    result = _run(["findmnt", "-rn", "-o", "SOURCE", "--target", path], check=False)
    if result.returncode != 0:
        return None
    value = result.stdout.strip().splitlines()
    return value[0] if value else None


def device_has_mounts(device: str) -> bool:
    result = _run(["findmnt", "-rn", "-S", device], check=False)
    return result.returncode == 0 and bool(result.stdout.strip())


def bind_publish(device: str, target_path: str, readonly: bool = False) -> None:
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.touch(mode=0o600)

    if is_mountpoint(target_path):
        source = mounted_source(target_path)
        if source == device:
            return
        raise NVMeError(f"target {target_path} is already mounted from {source}")

    _run(["mount", "--bind", device, target_path])
    if readonly:
        _run(["mount", "-o", "remount,bind,ro", target_path])


def unpublish(target_path: str) -> None:
    target = Path(target_path)
    if is_mountpoint(target_path):
        _run(["umount", target_path])
    try:
        if target.exists() and not target.is_dir():
            target.unlink()
    except OSError as exc:
        raise NVMeError(f"failed to remove target path {target_path}: {exc}") from exc


def save_state(state_dir: str, volume_id: str, state: dict[str, object]) -> None:
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{volume_id}.json"
    tmp = directory / f".{volume_id}.json.tmp"
    tmp.write_text(json.dumps(state, sort_keys=True))
    os.replace(tmp, path)


def load_state(state_dir: str, volume_id: str) -> dict[str, object] | None:
    path = Path(state_dir) / f"{volume_id}.json"
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise NVMeError(f"failed to read state for {volume_id}: {exc}") from exc


def delete_state(state_dir: str, volume_id: str) -> None:
    path = Path(state_dir) / f"{volume_id}.json"
    try:
        path.unlink()
    except FileNotFoundError:
        pass
