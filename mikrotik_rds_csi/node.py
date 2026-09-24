from __future__ import annotations

from pathlib import Path
import grpc

from .config import NodeConfig
from .generated import csi_pb2, csi_pb2_grpc
from . import nvme


SUPPORTED_ACCESS_MODES = {
    csi_pb2.VolumeCapability.AccessMode.SINGLE_NODE_WRITER,
}


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
        if not request.volume_capability.HasField("block"):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "only raw Block volumes are supported")
        if request.volume_capability.access_mode.mode not in SUPPORTED_ACCESS_MODES:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "unsupported access mode")

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
            nvme.bind_publish(device, request.target_path, readonly=request.readonly)
            nvme.save_state(
                self.config.state_dir,
                request.volume_id,
                {
                    "nqn": nqn,
                    "target": target,
                    "port": int(port),
                    "nsid": int(nsid),
                    "device": device,
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
        # No STAGE_UNSTAGE capability: this MVP connects and bind-publishes
        # directly in NodePublishVolume.
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
            result = nvme._run(["blockdev", "--getsize64", request.volume_path])
            total = int(result.stdout.strip())
        except (nvme.NVMeError, ValueError) as exc:
            context.abort(grpc.StatusCode.INTERNAL, str(exc))
        return csi_pb2.NodeGetVolumeStatsResponse(
            usage=[
                csi_pb2.VolumeUsage(
                    total=total,
                    available=total,
                    used=0,
                    unit=csi_pb2.VolumeUsage.BYTES,
                )
            ]
        )
