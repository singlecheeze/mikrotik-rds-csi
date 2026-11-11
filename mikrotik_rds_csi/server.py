from __future__ import annotations

from concurrent import futures
import argparse
import logging
import os
from pathlib import Path
import signal

import grpc


LOG = logging.getLogger(__name__)


def _socket_target(endpoint: str) -> tuple[str, Path | None]:
    if endpoint.startswith("unix://"):
        path = Path(endpoint[len("unix://"):])
        return f"unix://{path}", path
    return endpoint, None


def serve_csi(mode: str, endpoint: str) -> None:
    # Lazy imports keep the diagnostic/status modes independent from the gRPC
    # service startup path while still shipping in the same image.
    from .config import ControllerConfig, NodeConfig
    from .controller import ControllerService
    from .generated import csi_pb2_grpc
    from .identity import IdentityService
    from .node import NodeService

    target, socket_path = _socket_target(endpoint)
    if socket_path:
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=16))
    csi_pb2_grpc.add_IdentityServicer_to_server(IdentityService(), server)

    if mode == "controller":
        csi_pb2_grpc.add_ControllerServicer_to_server(
            ControllerService(ControllerConfig.from_env()), server
        )
    elif mode == "node":
        csi_pb2_grpc.add_NodeServicer_to_server(NodeService(NodeConfig.from_env()), server)
    else:
        raise ValueError(f"unsupported CSI mode: {mode}")

    if server.add_insecure_port(target) == 0:
        raise RuntimeError(f"failed to bind CSI endpoint {endpoint}")

    server.start()
    LOG.info("MikroTik RDS CSI %s service listening on %s", mode, endpoint)

    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        if stopping:
            return
        stopping = True
        LOG.info("received signal %s, stopping", signum)
        server.stop(grace=5)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    server.wait_for_termination()


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["controller", "node", "node-status", "status-api"],
        required=True,
    )
    parser.add_argument("--endpoint", default=os.getenv("CSI_ENDPOINT", "unix:///csi/csi.sock"))
    parser.add_argument("--listen-address", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--tls-cert", default=None)
    parser.add_argument("--tls-key", default=None)
    parser.add_argument("--static-dir", default=None)
    args = parser.parse_args()

    if args.mode in {"controller", "node"}:
        serve_csi(args.mode, args.endpoint)
        return
    if args.mode == "node-status":
        from .node_status import run_node_status_reporter

        run_node_status_reporter()
        return
    if args.mode == "status-api":
        from .status_api import StatusAPIConfig, serve_status_api

        config = StatusAPIConfig.from_env(
            static_dir=args.static_dir,
            listen_address=args.listen_address,
            port=args.port,
            tls_cert=args.tls_cert,
            tls_key=args.tls_key,
        )
        serve_status_api(config)
        return
    raise RuntimeError(f"unsupported mode {args.mode}")


if __name__ == "__main__":
    main()
