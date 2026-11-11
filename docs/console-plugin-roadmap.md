# OpenShift console plugin implementation and roadmap

Version `0.4.0` implements the original **Phase 1** and **Phase 2** goals for the MikroTik RDS CSI OpenShift console integration. Phase 3 (Prometheus metrics/history) remains future work.

## Implemented in v0.4.0

The optional console integration is read-only and consists of three pieces:

1. `console-plugin/` - OpenShift 4.22 dynamic-plugin source using the console SDK, React 18, Webpack 5, and PatternFly 6.
2. `mikrotik_rds_csi/status_api.py` - HTTPS status/API aggregator and static plugin asset server.
3. `mikrotik_rds_csi/node_status.py` - per-node route/TCP/NVMe diagnostic collector, deployed as a standalone DaemonSet by `deploy/console/`.

The optional deployment is additive. It assumes the base CSI driver is already installed and does not reapply the base CSI ConfigMap or other deployment-specific storage settings.

## Architecture

```text
OpenShift web console
        |
        | ConsolePlugin backend + proxy
        | authorization: UserToken
        v
mikrotik-rds-csi-console Service :9443 (HTTPS)
        |
        +-- dynamic-plugin static assets
        |
        +-- /api/v1/* status API
              |
              +-- Kubernetes API (read-only)
              |     Deployment / DaemonSet / CSINode / StorageClass
              |     PV / PVC / Pod / Event / node-status ConfigMap
              |
              +-- RouterOS REST
                    existing rds-credentials Secret
                    existing rds-rest-ca ConfigMap

mikrotik-rds-csi-node-status DaemonSet
        |
        +-- hostNetwork
        +-- host /sys read-only
        +-- ip route get <target>
        +-- route source/interface + MTU/operstate
        +-- short TCP connect to target:4420
        +-- existing NVMe/TCP controller inspection
        +-- node-status ConfigMap
```

RouterOS credentials never leave the backend container. The browser accesses the status API only through the OpenShift console proxy. The backend checks the forwarded user token with `SelfSubjectAccessReview` before returning diagnostics.

## Phase 1 - implemented

### Cluster overview

The Overview tab reports:

- CSI controller Deployment Ready/desired replicas;
- CSI node DaemonSet Ready/desired pods;
- `rds.csi.mikrotik.com` CSINode registration relative to the CSI DaemonSet's expected node count;
- existence/provisioner match for `mikrotik-rds-nvme`;
- RDS REST and pool health;
- NVMe/TCP target reachability across expected CSI nodes;
- usable RDS capacity;
- recent CSI/NVMe/mount warning events.

The registration calculation intentionally does **not** compare the driver against every CSINode object in the cluster. Control-plane or otherwise untargeted nodes can legitimately have a CSINode object without running this CSI DaemonSet.

### Volume inventory

The Volumes tab filters PVs to `spec.csi.driver=rds.csi.mikrotik.com` and shows:

- namespace/PVC with OpenShift console link;
- PV with OpenShift console link;
- `Block` or `Filesystem` mode;
- capacity;
- CSI volume handle / RouterOS slot;
- NQN;
- target address and port;
- PVC/PV phase;
- reclaim policy;
- detected Pod consumer and node;
- detected KubeVirt VM name when a consuming virt-launcher pod carries the normal VM labels.

### Recent warnings

The backend reads cluster Warning events and retains recent entries matching the driver, NVMe, `MountVolume`, or registry-storage context. The default age window is 60 minutes.

## Phase 2 - implemented

### RDS status

The RDS tab uses the existing backend-only RouterOS credentials and CA to report:

- REST reachable/healthy;
- RouterOS version, board, and uptime;
- configured pool slot/path;
- pool state;
- mounted state;
- filesystem;
- total/free/usable bytes;
- configured reserve;
- number of managed `csi-<28 hex>` disks;
- number of active NVMe/TCP exports;
- configured NVMe/TCP target address/port.

### Per-node connectivity

The node reporter publishes one labeled ConfigMap per node at a default 15-second interval. It records:

| Field | Meaning |
|---|---|
| Node | NVMe/TCP initiator node |
| Route interface | interface returned by `ip route get <target>` |
| Source IP | exact route-selected source address used by the CSI design for `--host-traddr` |
| Configured-interface match | verifies `RDS_STORAGE_INTERFACE` when enforcement is configured |
| Interface MTU | read from host sysfs |
| Interface operstate | read from host sysfs |
| Target TCP | short source-bound connect to the configured NVMe/TCP port |
| Active controllers | existing TCP NVMe controllers for the RDS target |
| Connected NQNs | active RDS subsystem NQNs |
| Last report | age/staleness |
| Errors | current route/interface/TCP/report errors |

The central status service enriches those reports with the actual CSI node pod state and CSINode registration.

### Duplicate source-IP detection

Duplicate route source addresses are treated as a critical health condition. The UI identifies the source IP and every affected node. This directly covers the lab failure where multiple nodes accidentally reported `172.16.100.133`, causing intermittent NVMe/TCP connection failures.

### Lightweight diagnostics only

The periodic collector intentionally avoids:

- `nvme discover` loops;
- jumbo ping loops;
- filesystem probes or formatting;
- mount/unmount actions;
- RouterOS mutations.

It uses route inspection, sysfs reads, a short TCP connection, and existing-controller inspection only.

## UI layout

### Overview

Summary cards:

```text
RDS REST        Reachable / unavailable
CSI controller  ready / desired
Node plugins    ready / desired
CSI registered  registered / expected
NVMe target     reachable / expected
RDS capacity    usable bytes
```

Then:

1. duplicate-source-IP critical alerts, if any;
2. node connectivity table;
3. recent warning events.

### Nodes

Per-node table plus detail cards for:

- route output;
- configured interface;
- source IP and uniqueness;
- MTU/operstate;
- target reachability;
- CSI pod state;
- CSINode registration;
- connected NQNs;
- current errors and stale-report status.

### Volumes

RDS-backed PV/PVC table with direct links into existing OpenShift resource pages.

### RDS

RouterOS endpoint and storage-pool health/capacity only. Credentials and unrelated `/rest/file` contents are never returned.

## Status API

Implemented endpoints:

```text
GET /healthz             # Kubernetes probes only; no diagnostic data
GET /api/v1/health
GET /api/v1/overview
GET /api/v1/rds
GET /api/v1/nodes
GET /api/v1/volumes
GET /api/v1/events
```

The `/api/v1/*` endpoints require an OpenShift user bearer token forwarded by the `ConsolePlugin` proxy. The current authorization gate requires permission to list both cluster Nodes and PersistentVolumes before backend diagnostics are returned.

## Security model

- `rds-credentials` is mounted only into the console/status backend, never into browser assets or the node reporter.
- `rds-rest-ca` remains server-side and is used for RouterOS TLS verification.
- The console Service uses an OpenShift service-serving certificate.
- The console ServiceAccount receives read-only Kubernetes access for status aggregation.
- The existing CSI node ServiceAccount receives namespace-scoped write access only for labeled diagnostic ConfigMaps.
- The standalone diagnostic DaemonSet reuses the CSI node ServiceAccount because OpenShift must permit `hostNetwork` and read-only host `/sys`; the reporter container itself is not privileged, drops all Linux capabilities, and performs no write/mount operations on the host.
- No console endpoint performs `CreateVolume`, `DeleteVolume`, formatting, RAID changes, or backing-file deletion.

## Deployment files

```text
console-plugin/
Containerfile.console

deploy/console/
  01-rbac.yaml
  02-console.yaml
  03-node-status.yaml
  kustomization.yaml

mikrotik_rds_csi/
  kube.py
  node_status.py
  status_api.py
```

The deployment process and validation commands are documented in the main `README.md` section **OpenShift console plugin and status API (v0.4.0)**.

## Phase 3 - future: Prometheus metrics and history

The next useful expansion is historical monitoring rather than more active browser actions.

Suggested controller/status metrics:

```text
mikrotik_rds_rest_up
mikrotik_rds_pool_state
mikrotik_rds_pool_total_bytes
mikrotik_rds_pool_free_bytes
mikrotik_rds_pool_usable_bytes
mikrotik_rds_managed_volumes
mikrotik_rds_create_volume_total
mikrotik_rds_create_volume_errors_total
mikrotik_rds_delete_volume_total
mikrotik_rds_delete_volume_errors_total
```

Suggested node metrics:

```text
mikrotik_rds_node_target_reachable{node=...}
mikrotik_rds_node_route_source_info{node=...,source_ip=...,interface=...}
mikrotik_rds_node_interface_mtu{node=...,interface=...}
mikrotik_rds_node_nvme_controllers{node=...}
mikrotik_rds_node_publish_total{node=...}
mikrotik_rds_node_publish_errors_total{node=...}
```

A future `ServiceMonitor` could integrate these with OpenShift monitoring and allow the console plugin to display capacity, reachability, attach failures, and latency/error history.

## Later optional enhancements

After the read-only UI is proven in the lab, reasonable additions include:

- an explicit on-demand connectivity check with a clear audit trail;
- PVC/PV/Node resource tabs contributed by the plugin;
- filtering/sorting large volume inventories;
- per-volume attachment history;
- alert links into OpenShift monitoring;
- CSI sanity/conformance status.

Destructive RouterOS operations should remain outside the console plugin unless a much stronger authorization, confirmation, and audit design is added.
