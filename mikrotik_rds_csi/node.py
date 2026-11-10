from __future__ import annotations

import os
from pathlib import Path
import grpc

from .config import NodeConfig
from .generated import csi_pb2, csi_pb2_grpc
from . import nvme


SUPPORTED_ACCESS_MODES = {
    csi_pb2.VolumeCapability.AccessMode.SINGLE_NODE_WRITER,
}


def _volume_mode(capability) -> tuple[str, str, list[str]]:
    if capability.HasField("block"):
        return "block", "", []
    if capability.HasField("mount"):
        fs_type = (capability.mount.fs_type or "xfs").strip().lower()
        if fs_type != "xfs":
            raise ValueError(f"filesystem type {fs_type!r} is unsupported; use xfs")
        return "mount", fs_type, list(capability.mount.mount_flags)
    raise ValueError("volume_capability must specify block or mount")


class NodeService(csi_pb2_grpc.NodeServicer):
    def __init__(self, config: NodeConfig) -> None:
        self.config = config

    def NodePublishVolume(self, request, context):
        if not request.volume_id:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "volume_id is required")
        if not request.target_path:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "target_path is required")
        if not request.HasField("volume_capability"):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "volume_capability is required")
        if request.volume_capability.access_mode.mode not in SUPPORTED_ACCESS_MODES:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "unsupported access mode")

        try:
            volume_mode, fs_type, mount_flags = _volume_mode(request.volume_capability)
        except ValueError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))

        target = request.volume_context.get("target")
        port = request.volume_context.get("port", "4420")
        nqn = request.volume_context.get("nqn")
        nsid = request.volume_context.get("nsid", "1")
        if not target or not nqn:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "volume_context target and nqn are required")

        try:
            device = nvme.connect(
                target=target,
                port=int(port),
                nqn=nqn,
                nsid=int(nsid),
                timeout_seconds=self.config.connect_timeout_seconds,
                expected_interface=self.config.storage_interface or None,
                use_route_source_address=self.config.use_route_source_address,
                reconnect_delay_seconds=self.config.reconnect_delay_seconds,
                ctrl_loss_tmo_seconds=self.config.ctrl_loss_tmo_seconds,
                nvme_module=self.config.nvme_module,
            )

            if volume_mode == "block":
                nvme.bind_publish(device, request.target_path, readonly=request.readonly)
            else:
                nvme.mount_publish(
                    device,
                    request.target_path,
                    fs_type=fs_type,
                    mount_flags=mount_flags,
                    readonly=request.readonly,
                )

            nvme.save_state(
                self.config.state_dir,
                request.volume_id,
                {
                    "nqn": nqn,
                    "target": target,
                    "port": int(port),
                    "nsid": int(nsid),
                    "device": device,
                    "volume_mode": volume_mode,
                    "fs_type": fs_type,
                },
            )
            return csi_pb2.NodePublishVolumeResponse()
        except (nvme.NVMeError, ValueError) as exc:
            context.abort(grpc.StatusCode.INTERNAL, str(exc))

    def NodeUnpublishVolume(self, request, context):
        if not request.volume_id:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "volume_id is required")
        if not request.target_path:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "target_path is required")

        try:
            state = nvme.load_state(self.config.state_dir, request.volume_id) or {}
            nqn = str(state.get("nqn") or nvme.nqn_for_volume_id(request.volume_id) or "")
            nsid = int(state.get("nsid", 1) or 1)
            device = str(state.get("device") or (nvme.device_for_nqn(nqn, nsid) if nqn else "") or "")

            nvme.unpublish(request.target_path)

            if nqn and (not device or not nvme.device_has_mounts(device)):
                nvme.disconnect(nqn)
                nvme.delete_state(self.config.state_dir, request.volume_id)

            return csi_pb2.NodeUnpublishVolumeResponse()
        except (nvme.NVMeError, ValueError) as exc:
            context.abort(grpc.StatusCode.INTERNAL, str(exc))

    def NodeGetCapabilities(self, request, context):
        # No STAGE_UNSTAGE capability: this driver connects and publishes
        # directly in NodePublishVolume for both block and filesystem volumes.
        return csi_pb2.NodeGetCapabilitiesResponse(capabilities=[])

    def NodeGetInfo(self, request, context):
        return csi_pb2.NodeGetInfoResponse(node_id=self.config.node_id)

    def NodeGetVolumeStats(self, request, context):
        if not request.volume_path:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "volume_path is required")
        path = Path(request.volume_path)
        if not path.exists():
            context.abort(grpc.StatusCode.NOT_FOUND, f"volume path {request.volume_path} does not exist")

        try:
            if path.is_dir():
                stats = os.statvfs(path)
                total = stats.f_blocks * stats.f_frsize
                available = stats.f_bavail * stats.f_frsize
                used = total - (stats.f_bfree * stats.f_frsize)
            else:
                result = nvme._run(["blockdev", "--getsize64", request.volume_path])
                total = int(result.stdout.strip())
                available = total
                used = 0
        except (nvme.NVMeError, OSError, ValueError) as exc:
            context.abort(grpc.StatusCode.INTERNAL, str(exc))

        return csi_pb2.NodeGetVolumeStatsResponse(
            usage=[
                csi_pb2.VolumeUsage(
                    total=total,
                    available=available,
                    used=used,
                    unit=csi_pb2.VolumeUsage.BYTES,
                )
            ]
        )
