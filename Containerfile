FROM python:3.11-slim-bookworm AS build

ARG CSI_SPEC_VERSION=v1.12.0
WORKDIR /src

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY hack ./hack
COPY mikrotik_rds_csi ./mikrotik_rds_csi

RUN python -m pip install --no-cache-dir grpcio-tools==1.84.0 protobuf==7.36.2 \
    && CSI_SPEC_VERSION="${CSI_SPEC_VERSION}" bash ./hack/generate-proto.sh \
    && python -m pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.11-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates \
       iproute2 \
       kmod \
       nvme-cli \
       util-linux \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/*.whl \
    && rm -rf /wheels

ENTRYPOINT ["mikrotik-rds-csi"]
