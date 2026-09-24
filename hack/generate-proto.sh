#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CSI_SPEC_VERSION="${CSI_SPEC_VERSION:-v1.12.0}"
PROTO_DIR="${ROOT}/proto"
OUT_DIR="${ROOT}/mikrotik_rds_csi/generated"

mkdir -p "${PROTO_DIR}" "${OUT_DIR}"

curl -fsSL \
  "https://raw.githubusercontent.com/container-storage-interface/spec/${CSI_SPEC_VERSION}/csi.proto" \
  -o "${PROTO_DIR}/csi.proto"

python -m grpc_tools.protoc \
  -I "${PROTO_DIR}" \
  --python_out="${OUT_DIR}" \
  --grpc_python_out="${OUT_DIR}" \
  "${PROTO_DIR}/csi.proto"

python - "${OUT_DIR}/csi_pb2_grpc.py" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
text = path.read_text()
text = text.replace("import csi_pb2 as csi__pb2", "from . import csi_pb2 as csi__pb2")
path.write_text(text)
PY

echo "Generated CSI Python bindings from ${CSI_SPEC_VERSION}."
