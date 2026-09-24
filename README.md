# mikrotik-rds-csi

Experimental Python CSI driver for dynamically provisioning raw block storage from a MikroTik RDS / ROSE Data Server. The controller creates file-backed RouterOS disk objects through the RouterOS REST API, exports them with NVMe/TCP, and the node plugin connects those namespaces directly with the Linux `nvme_tcp` initiator.

The driver does **not** assume a particular RAID name, RDS management IP, NVMe/TCP target IP, network interface, NQN prefix, or backing-file path. Deployment-specific values live in a ConfigMap, and most per-volume backend settings can also be overridden by StorageClass parameters.

## Current scope

- dynamic `CreateVolume` and `DeleteVolume`
- real `GetCapacity` from the configured RDS pool
- raw `Block` PVCs
- `ReadWriteOnce` / CSI `SINGLE_NODE_WRITER`
- NVMe/TCP connect/disconnect on OpenShift nodes
- configurable TLS verification and custom CA
- configurable pool validation, capacity reserve, NQN, NSID, port, file extension, route-source behavior, reconnect timers, and optional interface enforcement
- no CSI controller attach step (`attachRequired: false`)
- no snapshots, clones, filesystem-mode PVCs, or expansion yet

## Architecture

```text
PVC
  |
  v
csi-provisioner
  |
  v
Python CSI controller
  |
  | HTTPS REST (configurable management endpoint)
  v
MikroTik RDS
  |
  +-- create <configured-volume-path>/csi-<id><configured-extension>
  +-- create RouterOS /disk type=file
  +-- nvme-tcp-export=true
  +-- deterministic configurable NQN

VM / Pod scheduled
  |
  v
Python CSI node DaemonSet
  |
  +-- load configured NVMe kernel module
  +-- follow route to per-volume targetAddress
  +-- optionally enforce a configured interface
  +-- nvme connect -> targetAddress:targetPort
  +-- resolve NQN + NSID -> /dev/nvmeXnY
  +-- bind-publish raw block device into kubelet target path
  |
  v
NVMe/TCP data path -> MikroTik RDS
```

Python is only in the provisioning and attach control path. Application I/O flows through the Linux NVMe/TCP kernel stack directly to the RDS.

## Versions used by this MVP

- OpenShift target: 4.22 / Kubernetes 1.35
- CSI protobuf: v1.12.0
- Python: 3.11+
- gRPC Python: 1.84.0
- external-provisioner: v6.3.0
- node-driver-registrar: v2.17.0

Generated CSI Python bindings are intentionally not checked in. `hack/generate-proto.sh` downloads the official CSI `csi.proto` and generates them during the container build or local development.

## Configuration model

Controller-wide defaults are read from environment variables. `deploy/openshift/01-config.yaml` exposes them through the `mikrotik-rds-csi-config` ConfigMap. The values marked **backend-specific** should be changed for each RDS installation.

The **Lab example** column shows the values used or directly validated in the development lab. For driver-only timing and local-state settings that are not properties reported by the RDS itself, the table shows the current lab/MVP setting.

| Environment variable | Default | Lab example | Purpose |
|---|---|---|---|
| `RDS_API_ENDPOINT` | required | `https://172.16.1.125` | RouterOS HTTPS REST endpoint, for example `https://rds.example.com` |
| `RDS_API_TIMEOUT_SECONDS` | `15` | `15` | REST request timeout |
| `RDS_TLS_VERIFY` | `true` | `true` | Verify the RouterOS TLS certificate |
| `RDS_CA_FILE` | empty internally | `/etc/rds-ca/ca.crt` | Optional custom CA file; the manifest uses `/etc/rds-ca/ca.crt` |
| `RDS_POOL_SLOT` | required at provision time | `raid10` | RouterOS `/disk` slot used for capacity/health checks |
| `RDS_POOL_PATH` | required at provision time | `/raid10` | Directory where CSI backing files are created; it may be the pool root or a pre-created subdirectory |
| `RDS_POOL_FILESYSTEM` | `xfs` | `xfs` | Expected filesystem; empty disables filesystem validation |
| `RDS_POOL_REQUIRED_STATE` | `clean` | `clean` | Expected pool state; empty disables state validation |
| `RDS_POOL_REQUIRE_MOUNTED` | `true` | `true` | Require `mounted=true` on the pool object |
| `RDS_RESERVE_BYTES` | `1073741824` | `1073741824` | Capacity held back from CSI `GetCapacity` and provisioning |
| `RDS_FILE_EXTENSION` | `.img` | `.img` | Backing-file extension; may be empty |
| `RDS_NVME_TARGET_ADDRESS` | required at provision time | `172.16.100.125` | NVMe/TCP data-plane address advertised to nodes |
| `RDS_NVME_TARGET_PORT` | `4420` | `4420` | NVMe/TCP target port |
| `RDS_NQN_PREFIX` | required at provision time | `nqn.2026-09.com.mikrotik:rds2216` | Prefix used to form `<prefix>.csi-<id>` |
| `RDS_NVME_NSID` | `1` | `1` | Namespace ID returned in CSI volume context |
| `RDS_CONNECT_TIMEOUT_SECONDS` | `20` | `20` | Node wait time for a namespace to appear |
| `RDS_STORAGE_INTERFACE` | empty | `bond1.100` | Optional interface that the route to the target must use; empty accepts the kernel-selected route |
| `RDS_USE_ROUTE_SOURCE_ADDRESS` | `true` | `true` | Derive `--host-traddr` from `ip route get`; disable if the environment does not require source binding |
| `RDS_NVME_RECONNECT_DELAY_SECONDS` | `10` | `10` | `nvme connect --reconnect-delay` |
| `RDS_NVME_CTRL_LOSS_TMO_SECONDS` | `600` | `600` | `nvme connect --ctrl-loss-tmo` |
| `RDS_NVME_MODULE` | `nvme_tcp` | `nvme_tcp` | Kernel module loaded before connecting |
| `RDS_NODE_STATE_DIR` | `/var/lib/mikrotik-rds-csi` | `/var/lib/mikrotik-rds-csi` | Persistent node-plugin state directory |

For backward compatibility, `RDS_STORAGE_TARGET` and `RDS_STORAGE_PORT` are accepted as aliases for `RDS_NVME_TARGET_ADDRESS` and `RDS_NVME_TARGET_PORT`.

### StorageClass overrides

One controller deployment can expose different RDS pools or data-plane endpoints by overriding the global defaults in a StorageClass. Supported provisioner parameters are:

```yaml
parameters:
  poolSlot: "my-pool-slot"
  poolPath: "/my-pool-or-existing-subdirectory"
  poolFilesystem: "xfs"
  poolRequiredState: "clean"
  requirePoolMounted: "true"
  reserveBytes: "1073741824"
  fileExtension: ".img"
  targetAddress: "192.0.2.25"
  targetPort: "4420"
  nqnPrefix: "nqn.2026-09.example:rds"
  nsid: "1"
```

Any omitted parameter falls back to the ConfigMap/environment default. The RouterOS management endpoint and credentials are controller-wide in this release.

## 1. Configure and validate RouterOS REST access

The CSI controller uses the RouterOS REST API over HTTPS. RouterOS REST is provided by the `www-ssl` service; the separate `api` and `api-ssl` services on ports 8728/8729 are **not required** by this driver.

The examples below use the values validated in the development lab:

```text
RDS management IP:     172.16.1.125
Allowed management net: 172.16.1.0/24
REST HTTPS port:        443
CA name:                rds-rest-ca
Server cert name:       rds-rest-server
```

Replace the management IP/subnet and certificate names as appropriate for your environment.

### 1.1 Check the RouterOS web/API services

On the RDS, inspect the relevant services:

```routeros
/ip/service/print detail where name~"www|api"
```

For REST over HTTPS, `www-ssl` must be enabled. `api` and `api-ssl` may remain disabled because they are the native RouterOS API, not the REST API.

Also check the RouterOS web-server feature flags:

```routeros
/ip/service/webserver/print
```

Confirm:

```text
rest-secure: yes
```

If needed, enable secure REST:

```routeros
/ip/service/webserver/set rest-secure=yes
```

Plain HTTP REST (`rest-plain`) is not required for the CSI driver.

### 1.2 Check for an existing HTTPS certificate

```routeros
/certificate/print detail
```

If `www-ssl` already uses a valid certificate whose name/IP is trusted by the CSI controller, reuse it and skip to **1.5**. Otherwise, create a local CA and an RDS server certificate as shown below.

### 1.3 Create and sign a local CA

Create the CA template:

```routeros
/certificate/add \
    name=rds-rest-ca \
    common-name=rds-rest-ca \
    key-usage=key-cert-sign,crl-sign
```

The CA must be signed **before** it can be used to sign the server certificate:

```routeros
/certificate/sign rds-rest-ca
```

Verify it:

```routeros
/certificate/print detail where name="rds-rest-ca"
```

A locally generated CA should show the private-key, authority, and trusted flags (`KAT`).

### 1.4 Create and sign the REST server certificate

The certificate should contain the RDS management IP or DNS name in its Subject Alternative Name. The validated lab uses `172.16.1.125`:

```routeros
/certificate/add \
    name=rds-rest-server \
    common-name=172.16.1.125 \
    subject-alt-name=IP:172.16.1.125 \
    key-usage=tls-server
```

Sign it with the CA:

```routeros
/certificate/sign \
    rds-rest-server \
    ca=rds-rest-ca
```

Verify the chain and SAN:

```routeros
/certificate/print detail where name~"rds-rest"
```

The server certificate should report `ca=rds-rest-ca` and `subject-alt-name=IP:172.16.1.125` (or your configured DNS/IP SAN).

### 1.5 Enable `www-ssl` and restrict management access

Assign the certificate to `www-ssl`, enable the service, and restrict it to the trusted management network. On the RouterOS 7.24.x RDS used in the lab, the current property is `available-from`:

```routeros
/ip/service/set [find where name="www-ssl"] \
    certificate=rds-rest-server \
    disabled=no \
    available-from=172.16.1.0/24
```

Verify:

```routeros
/ip/service/print detail where name="www-ssl"
```

Expected lab-style result:

```text
name="www-ssl" port=443 proto=tcp available-from=172.16.1.0/24 certificate=rds-rest-server
```

> **RouterOS version note:** some RouterOS documentation/releases show this restriction as `address=` instead of `available-from=`. Use the property shown by `/ip/service/print detail` on your RDS.

### 1.6 Export the CA certificate

Export the CA as PEM so the OpenShift CSI controller can validate the RDS certificate without disabling TLS verification:

```routeros
/certificate/export-certificate \
    rds-rest-ca \
    type=pem \
    file-name=rds-rest-ca
```

Confirm the exported file exists:

```routeros
/file/print where name~"rds-rest-ca"
```

Copy/download the exported CA certificate to the system from which you will deploy the CSI driver. In the examples below it is saved as `rds-rest-ca.crt`.

### 1.7 Verify REST over HTTPS before creating the CSI account

From a client on the allowed management network, first test HTTPS while ignoring CA validation. This is only a connectivity/certificate-bootstrap test:

```bash
curl -kv \
  --connect-timeout 5 \
  -u admin \
  https://172.16.1.125/rest/system/resource
```

A working REST endpoint should return:

```text
HTTP/1.1 200 OK
Content-Type: application/json
```

followed by the RDS system-resource JSON.

Next verify the storage endpoint that the CSI controller actually needs:

```bash
curl -sk \
  --connect-timeout 5 \
  -u admin \
  https://172.16.1.125/rest/disk | jq
```

The result should include the RDS hardware disks, configured RAID/pool objects, and any existing file-backed disk exports.

### 1.8 Create the dedicated CSI RouterOS account

Do not use `admin` for the deployed CSI driver. Create a dedicated account with the REST permissions needed by the controller:

```routeros
/user/group/add \
    name=openshift-csi \
    policy=read,write,test,api,rest-api

/user/add \
    name=openshift-csi \
    group=openshift-csi \
    password="REPLACE_WITH_STRONG_PASSWORD"
```

> **RouterOS 7.24.x permission note:** the `test` policy is required for some read-only system/monitoring operations exposed through REST, including the `/system/resource` validation used below. Without it, authentication can succeed but RouterOS can return `std failure: not allowed (9)`.

If the group already exists from an earlier version of this README, update it in place:

```routeros
/user/group/set [find where name="openshift-csi"] \
    policy=read,write,test,api,rest-api
```

Verify the effective policy:

```routeros
/user/group/print detail where name="openshift-csi"
```

Then verify the dedicated user and CA chain together, without `-k`:

```bash
curl \
  --cacert ./rds-rest-ca.crt \
  --connect-timeout 5 \
  -u openshift-csi \
  https://172.16.1.125/rest/system/resource | jq
```

A successful response should contain the RDS system-resource JSON rather than `std failure: not allowed (9)`.

Next validate each REST resource used by the CSI controller:

```bash
# Storage inventory, capacity, pool health, and exported disks
curl \
  --cacert ./rds-rest-ca.crt \
  --connect-timeout 5 \
  -u openshift-csi \
  https://172.16.1.125/rest/disk | jq

# Backing-file lookup and cleanup during DeleteVolume
curl \
  --cacert ./rds-rest-ca.crt \
  --connect-timeout 5 \
  -u openshift-csi \
  https://172.16.1.125/rest/file | jq
```

The CSI account requires these effective RouterOS policies:

| RouterOS policy | Why the CSI account needs it |
|---|---|
| `rest-api` | Authenticate and access `/rest/*` |
| `read` | Inspect system, pool, disk, and file state |
| `write` | Create/update/delete CSI-managed file-backed `/disk` objects and backing files |
| `test` | Required by RouterOS 7.24.x for `/system/resource` validation and related monitoring operations |

Do **not** grant `policy`, `sensitive`, `ssh`, `ftp`, `api`, or `full` unless a future feature specifically requires them.

### 1.9 Disable plain HTTP

The CSI driver should use HTTPS only. After HTTPS REST is working, disable the plain `www` service:

```routeros
/ip/service/set [find where name="www"] disabled=yes
```

Optionally disable the plain REST feature as an additional hardening step if it is not used by anything else:

```routeros
/ip/service/webserver/set rest-plain=no
```

Confirm the final state:

```routeros
/ip/service/print detail where name~"www|api"
/ip/service/webserver/print
```

For this CSI driver the desired state is:

```text
www:        disabled
www-ssl:    enabled on TCP/443 with the REST server certificate
rest-secure: yes
api:        not required
api-ssl:    not required
```

### Optional: temporary plain-HTTP troubleshooting

If HTTPS is not yet configured and you only need to prove that REST itself responds, temporarily enable `www` and `rest-plain` on a trusted management network:

```routeros
/ip/service/set [find where name="www"] \
    disabled=no \
    available-from=172.16.1.0/24

/ip/service/webserver/set rest-plain=yes
```

Then test:

```bash
curl -v \
  --connect-timeout 5 \
  -u admin \
  http://172.16.1.125/rest/system/resource
```

Once the test succeeds, configure HTTPS as described above and disable `www` again. Do not leave Basic-authenticated REST exposed over plain HTTP.

### RouterOS REST references

- MikroTik RouterOS REST API: https://manual.mikrotik.com/docs/developer-guides/rest-api/
- RouterOS IP services and web-server REST flags: https://help.mikrotik.com/docs/spaces/ROS/pages/103841820/Services
- RouterOS certificates: https://manual.mikrotik.com/docs/authentication-authorization-accounting/certificates/

### Troubleshoot `not allowed (9)`

If REST works as `admin` but the dedicated CSI user receives:

```json
{
  "detail": "std failure: not allowed (9)",
  "error": 500,
  "message": "Internal Server Error"
}
```

and the RDS log shows a failure `via api`, add the native `api` policy in addition to `rest-api`:

```routeros
/user/group/set [find where name="openshift-csi"] \
    policy=read,write,test,api,rest-api
```

Then verify:

```routeros
/user/group/print detail where name="openshift-csi"
```

Expected policy set:

```text
read,write,test,api,rest-api
```

The `web` policy is not required for REST; `www-ssl` is the transport service, while user authorization is controlled by the API policies.

## 2. Configure the OpenShift manifests

Edit:

```text
deploy/openshift/01-config.yaml
```

At minimum set the management endpoint, pool slot, backing-file path, NVMe/TCP target address, and NQN prefix. `RDS_STORAGE_INTERFACE` is intentionally empty by default so the driver is not tied to a particular VLAN or Linux interface name.

If the RDS certificate uses a private CA, create the ConfigMap after creating the CSI namespace:

```bash
oc apply -f deploy/openshift/00-namespace.yaml

oc -n mikrotik-rds-csi create configmap rds-rest-ca \
  --from-file=ca.crt=./rds-rest-ca.crt \
  --dry-run=client -o yaml | oc apply -f -
```

If the certificate chains to a CA already trusted by the container image, set `RDS_CA_FILE` to an empty string and the `rds-rest-ca` ConfigMap is not required. For temporary lab testing only, `RDS_TLS_VERIFY=false` disables certificate verification.

Create the API credentials:

```bash
read -rsp 'RDS CSI password: ' RDS_PASSWORD; echo

oc -n mikrotik-rds-csi create secret generic rds-credentials \
  --from-literal=username=openshift-csi \
  --from-literal=password="${RDS_PASSWORD}" \
  --dry-run=client -o yaml | oc apply -f -

unset RDS_PASSWORD
```

## 3. Validate the configured RDS

The helper script is generic and requires the pool slot to be supplied:

```bash
export RDS_API_ENDPOINT=https://rds.example.com
export RDS_USERNAME=openshift-csi
export RDS_POOL_SLOT=my-pool
export RDS_TLS_VERIFY=true
export RDS_CA_FILE=./rds-rest-ca.crt

./hack/check-rds.sh
```

It prints the configured pool object and any current file-backed RouterOS disks without assuming a pool name or IP address.

## 4. Generate protobuf bindings for local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
make generate
make test
```

The container build performs protobuf generation automatically.

## 5. Build and push the image

```bash
IMAGE=quay.io/YOUR_USER/mikrotik-rds-csi:0.2.0
podman build -f Containerfile -t "${IMAGE}" .
podman push "${IMAGE}"
```

Update `deploy/openshift/kustomization.yaml`:

```yaml
images:
  - name: quay.io/REPLACE_ME/mikrotik-rds-csi
    newName: quay.io/YOUR_USER/mikrotik-rds-csi
    newTag: 0.2.0
```

## 6. Deploy

```bash
oc apply -k deploy/openshift
```

Verify:

```bash
oc -n mikrotik-rds-csi get pods -o wide
oc get csidriver rds.csi.mikrotik.com
oc get sc mikrotik-rds-nvme
oc get csinode -o yaml | grep -B3 -A3 rds.csi.mikrotik.com
```

The node DaemonSet has no custom node selector. Use standard Kubernetes/OpenShift scheduling controls if the CSI node plugin should run on only a subset of nodes.

## 7. OpenShift Virtualization StorageProfile

After CDI notices the StorageClass:

```bash
oc get storageprofile mikrotik-rds-nvme -o yaml
```

The intended claim properties are raw block + RWO. If CDI does not infer them automatically:

```bash
oc patch storageprofile mikrotik-rds-nvme --type=merge -p '
{
  "spec": {
    "claimPropertySets": [
      {
        "accessModes": ["ReadWriteOnce"],
        "volumeMode": "Block"
      }
    ]
  }
}'
```

## 8. Dynamic PVC test

```bash
oc apply -f deploy/openshift/06-test-pvc.yaml
oc -n nvme-test get pvc rds-csi-test -w
```

A successful provision creates a RouterOS file disk whose path, NQN prefix, target address, target port, and NSID all come from the resolved ConfigMap/StorageClass configuration.

Controller logs:

```bash
oc -n mikrotik-rds-csi logs deploy/mikrotik-rds-csi-controller -c csi-driver -f
```

Provisioner logs:

```bash
oc -n mikrotik-rds-csi logs deploy/mikrotik-rds-csi-controller -c csi-provisioner -f
```

To exercise node publish after the PVC is `Bound`:

```bash
oc apply -f deploy/openshift/07-test-pod.yaml
oc -n nvme-test get pod rds-csi-test -w
```

## Delete behavior and safety

RouterOS deletes the `/disk` object separately from its file-backed image. The driver therefore records the actual `file-path` from the RouterOS disk before deleting the export, removes the disk object, then removes that exact backing file. CSI volume IDs are deterministic `csi-<32 hex>` identifiers, and `DeleteVolume` refuses unmanaged IDs.

This remains an experimental driver. Before production use, add CSI conformance/sanity testing, controller leader election/HA, stronger backend ownership metadata, failure-injection tests, node-reboot recovery tests, and snapshot/clone/expansion support as required.
