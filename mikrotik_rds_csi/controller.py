from __future__ import annotations

import grpc

from .config import ControllerConfig, VolumeSettings
from .generated import csi_pb2, csi_pb2_grpc
from .naming import is_managed_volume_id, nqn_for_volume, volume_id_from_name
from .routeros import RouterOSClient, RouterOSError


SUPPORTED_ACCESS_MODES = {
    csi_pb2.VolumeCapability.AccessMode.SINGLE_NODE_WRITER,
}


def _is_supported_capability(capability) -> bool:
    if not capability.HasField("block"):
        return False
    if not capability.HasField("access_mode"):
        return False
    return capability.access_mode.mode in SUPPORTED_ACCESS_MODES


def _requested_bytes(request) -> int:
    if not request.HasField("capacity_range"):
        return 1024**3
    required = int(request.capacity_range.required_bytes or 0)
    limit = int(request.capacity_range.limit_bytes or 0)
    size = required if required > 0 else 1024**3
    if limit > 0 and size > limit:
        raise ValueError("required_bytes is greater than limit_bytes")
    return size


def _normalized_path(value: str) -> str:
    return value.lstrip("/").rstrip("/")


class ControllerService(csi_pb2_grpc.ControllerServicer):
    def __init__(self, config: ControllerConfig, rds: RouterOSClient | None = None) -> None:
        self.config = config
        self.rds = rds or RouterOSClient(
            config.api_endpoint,
            config.username,
            config.password,
            verify=config.requests_verify,
            timeout=config.api_timeout_seconds,
        )

    def _abort_routeros(self, context, exc: RouterOSError):
        context.abort(grpc.StatusCode.UNAVAILABLE, str(exc))

    def _settings(self, parameters, context) -> VolumeSettings:
        try:
            return self.config.volume_settings(dict(parameters))
        except RuntimeError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))

    @staticmethod
    def _volume_context(volume_id: str, settings: VolumeSettings) -> dict[str, str]:
        return {
            "target": settings.storage_target,
            "port": str(settings.storage_port),
            "nqn": nqn_for_volume(settings.nqn_prefix, volume_id),
            "nsid": str(settings.nsid),
        }

    def _check_pool(self, settings: VolumeSettings, requested_bytes: int = 0) -> int:
        status = self.rds.get_pool_status(settings.pool_slot)
        if settings.require_pool_mounted and not status.mounted:
            raise RouterOSError(f"RDS pool {status.slot} is not mounted")
        if (
            settings.pool_required_state
            and status.state.lower() != settings.pool_required_state.lower()
        ):
            raise RouterOSError(
                f"RDS pool {status.slot} state is {status.state!r}, "
                f"expected {settings.pool_required_state!r}"
            )
        if (
            settings.pool_filesystem
            and status.filesystem.lower() != settings.pool_filesystem.lower()
        ):
            raise RouterOSError(
                f"RDS pool {status.slot} filesystem is {status.filesystem!r}, "
                f"expected {settings.pool_filesystem!r}"
            )
        usable = max(0, status.free_bytes - settings.reserve_bytes)
        if requested_bytes and usable < requested_bytes:
            raise RouterOSError(
                f"insufficient capacity in {status.slot}: "
                f"requested={requested_bytes}, usable={usable}"
            )
        return usable

    def CreateVolume(self, request, context):
        if not request.name:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "name is required")
        if not request.volume_capabilities:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "volume_capabilities is required")
        if not all(_is_supported_capability(cap) for cap in request.volume_capabilities):
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "only raw Block volumes with SINGLE_NODE_WRITER are supported",
            )

        try:
            size = _requested_bytes(request)
        except ValueError as exc:
            context.abort(grpc.StatusCode.OUT_OF_RANGE, str(exc))

        settings = self._settings(request.parameters, context)
        volume_id = volume_id_from_name(request.name)

        try:
            existing = self.rds.get_disk_by_slot(volume_id)

            file_path = settings.file_path(volume_id)
            nqn = nqn_for_volume(settings.nqn_prefix, volume_id)

            if existing:
                existing_size = int(existing.get("file-size", existing.get("size", 0)) or 0)
                if existing_size < size:
                    context.abort(
                        grpc.StatusCode.ALREADY_EXISTS,
                        f"volume {request.name} already exists with capacity "
                        f"{existing_size}, requested {size}",
                    )

                existing_path = str(existing.get("file-path", ""))
                if existing_path and _normalized_path(existing_path) != _normalized_path(file_path):
                    context.abort(
                        grpc.StatusCode.ALREADY_EXISTS,
                        f"volume {request.name} already exists at {existing_path}, "
                        f"requested path is {file_path}",
                    )

                existing_nqn = str(existing.get("nvme-tcp-server-nqn", ""))
                existing_port = str(existing.get("nvme-tcp-server-port", ""))
                exported = str(existing.get("nvme-tcp-export", "false")).lower() == "true"

                if existing_nqn and existing_nqn != nqn:
                    context.abort(
                        grpc.StatusCode.ALREADY_EXISTS,
                        f"volume {request.name} already exists with NQN {existing_nqn}, "
                        f"requested NQN is {nqn}",
                    )
                if existing_port and existing_port != str(settings.storage_port):
                    context.abort(
                        grpc.StatusCode.ALREADY_EXISTS,
                        f"volume {request.name} already exists on port {existing_port}, "
                        f"requested port is {settings.storage_port}",
                    )

                disk_id = str(existing[".id"])
                if not exported or not existing_nqn or not existing_port:
                    self.rds.enable_nvme_export(disk_id, nqn, settings.storage_port)
                actual_size = existing_size
            else:
                self._check_pool(settings, size)
                created = self.rds.create_file_disk(volume_id, file_path, size)
                disk_id = str(created[".id"])
                exported = self.rds.enable_nvme_export(disk_id, nqn, settings.storage_port)
                actual_size = int(exported.get("file-size", exported.get("size", size)) or size)

            return csi_pb2.CreateVolumeResponse(
                volume=csi_pb2.Volume(
                    volume_id=volume_id,
                    capacity_bytes=actual_size,
                    volume_context=self._volume_context(volume_id, settings),
                )
            )
        except RouterOSError as exc:
            self._abort_routeros(context, exc)

    def DeleteVolume(self, request, context):
        if not request.volume_id:
            return csi_pb2.DeleteVolumeResponse()

        volume_id = request.volume_id
        if not is_managed_volume_id(volume_id):
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                f"refusing to delete unmanaged volume_id {volume_id!r}",
            )

        try:
            disk = self.rds.get_disk_by_slot(volume_id)
            backing_path = ""
            if disk:
                backing_path = str(disk.get("file-path", ""))
                disk_id = str(disk[".id"])
                if str(disk.get("nvme-tcp-export", "false")).lower() == "true":
                    self.rds.disable_nvme_export(disk_id)
                self.rds.delete_disk(disk_id)

            # RouterOS removes the /disk object but leaves the file-backed image.
            # Use the file path recorded on the disk object so deletion remains
            # correct even if deployment defaults or StorageClass parameters have
            # changed since the volume was created.
            if backing_path:
                backing = self.rds.get_file_by_name(backing_path)
                if backing:
                    self.rds.delete_file(str(backing[".id"]))
            elif not disk:
                # Recovery path for an interrupted prior DeleteVolume where the
                # /disk object is already gone but RouterOS left the backing file.
                # Match only the exact managed volume-id basename (with any
                # extension) and refuse ambiguous results across multiple paths.
                candidates = self.rds.find_files_for_volume_id(volume_id)
                if len(candidates) > 1:
                    raise RouterOSError(
                        f"multiple backing files match {volume_id}; refusing ambiguous cleanup"
                    )
                if candidates:
                    self.rds.delete_file(str(candidates[0][".id"]))

            return csi_pb2.DeleteVolumeResponse()
        except RouterOSError as exc:
            self._abort_routeros(context, exc)

    def ValidateVolumeCapabilities(self, request, context):
        if not request.volume_id:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "volume_id is required")
        if not is_managed_volume_id(request.volume_id):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "unmanaged volume_id")
        if not request.volume_capabilities:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "volume_capabilities is required")

        try:
            disk = self.rds.get_disk_by_slot(request.volume_id)
        except RouterOSError as exc:
            self._abort_routeros(context, exc)
        if not disk:
            context.abort(grpc.StatusCode.NOT_FOUND, f"volume {request.volume_id} does not exist")

        if all(_is_supported_capability(cap) for cap in request.volume_capabilities):
            return csi_pb2.ValidateVolumeCapabilitiesResponse(
                confirmed=csi_pb2.ValidateVolumeCapabilitiesResponse.Confirmed(
                    volume_capabilities=request.volume_capabilities,
                    volume_context=request.volume_context,
                    parameters=request.parameters,
                )
            )
        return csi_pb2.ValidateVolumeCapabilitiesResponse(
            message="only raw Block volumes with SINGLE_NODE_WRITER are supported"
        )

    def GetCapacity(self, request, context):
        settings = self._settings(request.parameters, context)
        try:
            available = self._check_pool(settings)
            return csi_pb2.GetCapacityResponse(available_capacity=available)
        except RouterOSError as exc:
            self._abort_routeros(context, exc)

    def ControllerGetCapabilities(self, request, context):
        caps = []
        for capability_type in (
            csi_pb2.ControllerServiceCapability.RPC.CREATE_DELETE_VOLUME,
            csi_pb2.ControllerServiceCapability.RPC.GET_CAPACITY,
        ):
            caps.append(
                csi_pb2.ControllerServiceCapability(
                    rpc=csi_pb2.ControllerServiceCapability.RPC(type=capability_type)
                )
            )
        return csi_pb2.ControllerGetCapabilitiesResponse(capabilities=caps)
