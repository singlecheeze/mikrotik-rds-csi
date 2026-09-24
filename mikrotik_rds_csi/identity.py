from __future__ import annotations

from google.protobuf import wrappers_pb2

from . import __version__
from .generated import csi_pb2, csi_pb2_grpc


DRIVER_NAME = "rds.csi.mikrotik.com"


class IdentityService(csi_pb2_grpc.IdentityServicer):
    def GetPluginInfo(self, request, context):
        return csi_pb2.GetPluginInfoResponse(
            name=DRIVER_NAME,
            vendor_version=__version__,
        )

    def GetPluginCapabilities(self, request, context):
        capability = csi_pb2.PluginCapability(
            service=csi_pb2.PluginCapability.Service(
                type=csi_pb2.PluginCapability.Service.CONTROLLER_SERVICE
            )
        )
        return csi_pb2.GetPluginCapabilitiesResponse(capabilities=[capability])

    def Probe(self, request, context):
        return csi_pb2.ProbeResponse(ready=wrappers_pb2.BoolValue(value=True))
