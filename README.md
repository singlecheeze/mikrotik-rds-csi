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
- Python: 3.10+ (Python 3.11 is used by the Containerfile and recommended for RHEL 9 development)
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
| `RDS_CA_FILE` | empty internally | `/etc/rds-ca/ca.crt` | Path **inside the CSI controller container** to a custom CA certificate. The supplied controller manifest mounts the `rds-rest-ca` ConfigMap at `/etc/rds-ca`, so its `ca.crt` key appears as `/etc/rds-ca/ca.crt`. Leave empty to use the container system trust store. |
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

## Optional RouterOS storage preparation and maintenance scripts

The repository includes the RouterOS storage helpers developed while building and validating this CSI driver under [`scripts/routeros/`](scripts/routeros/). These are **administrative/bootstrap tools**, not runtime dependencies of the CSI driver.

The included helpers are:

| File | Use |
|---|---|
| `build-ocp-raid10.rsc` | Builds the validated eight-disk nested RAID10 layout: four two-disk RAID1 mirrors striped by a top-level RAID0 with a `256K` chunk. |
| `format-xfs.commands.txt` | Manual XFS format command used after RAID synchronization. It is intentionally not automated because formatting is destructive and RouterOS requires confirmation. |
| `verify-ocp-storage.rsc` | Checks that the configured pool exists, is `clean`, is XFS, and is mounted before it is used for CSI backing files. |
| `trim-ocp-storage.rsc` | Performs a guarded `/disk trim` only after validating pool state, filesystem, and mount state. |
| `schedule-trim-weekly.rsc` | Installs the weekly 03:00 TRIM schedule used in the lab. |
| `create-ocp-vm-lun-example.rsc` | Retains the manual file-backed NVMe/TCP LUN workflow used during development; normally the CSI controller now performs these operations dynamically through REST. |
| `wipe-quick-nvme.commands.txt` | Manual destructive `wipe-quick` commands for the eight lab NVMe drives for complete rebuild/reset scenarios. |

The script defaults intentionally document the validated lab layout (`nvme1`-`nvme8`, `raid10`, `raid10-m0`-`raid10-m3`, `256K` RAID0 chunk, XFS, and port `4420`), but the RouterOS script files place installation-specific values near the top so they can be edited for another RDS.

For example, after uploading `build-ocp-raid10.rsc` to a new/empty RDS:

```routeros
/import file-name=build-ocp-raid10.rsc
/system/script/run build-ocp-raid10
```

The builder waits for the mirror arrays and top-level array to report `clean`, then prints the manual XFS format command. After formatting, the validation helper can be imported and run:

```routeros
/import file-name=verify-ocp-storage.rsc
/system/script/run verify-ocp-storage
```

For TRIM maintenance:

```routeros
/import file-name=trim-ocp-storage.rsc
/system/script/run trim-ocp-storage

# Only after the manual TRIM succeeds:
/import file-name=schedule-trim-weekly.rsc
```

> **Destructive operations:** RAID creation, `wipe-quick`, and filesystem formatting can destroy data. Review the physical disk names and target pool before running them. The format and wipe helpers are deliberately provided as manual command files rather than unattended scripts.

See [`scripts/routeros/README.md`](scripts/routeros/README.md) for the full sequence and per-script notes.

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
| `api` | Required on the validated RouterOS 7.24.4 RDS for REST requests; without it RouterOS logs the login attempt as `via api` and returns `std failure: not allowed (9)` |
| `read` | Inspect system, pool, disk, and file state |
| `write` | Create/update/delete CSI-managed file-backed `/disk` objects and backing files |
| `test` | Required by RouterOS 7.24.x for `/system/resource` validation and related monitoring operations |

The validated effective policy set is therefore `read,write,test,api,rest-api`. Do **not** grant `policy`, `sensitive`, `ssh`, `ftp`, or `full` unless a future feature specifically requires them.

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
native api service (TCP/8728):     not required
native api-ssl service (TCP/8729): not required
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

and the RDS log shows a failure `via api`, verify that the required native `api` policy is present in addition to `rest-api`:

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

> **Important:** the RouterOS `api` **user policy** is required for REST on the validated RDS/RouterOS 7.24.4 configuration. This does **not** mean the native RouterOS `api` service on TCP/8728 or `api-ssl` service on TCP/8729 must be enabled; the CSI driver still uses HTTPS REST through `www-ssl` on TCP/443.

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

### How the private CA ConfigMap and `RDS_CA_FILE` work together

These are two parts of the **same TLS configuration**, not two separate CA mechanisms.

The command above stores the local `./rds-rest-ca.crt` file in Kubernetes as a ConfigMap named `rds-rest-ca`, under the key `ca.crt`. The controller Deployment in `deploy/openshift/03-controller.yaml` mounts that ConfigMap read-only at `/etc/rds-ca`:

```yaml
volumeMounts:
  - name: rds-ca
    mountPath: /etc/rds-ca
    readOnly: true

volumes:
  - name: rds-ca
    configMap:
      name: rds-rest-ca
      optional: true
```

Because the ConfigMap key is named `ca.crt`, the mounted file inside the `csi-driver` container is:

```text
/etc/rds-ca/ca.crt
```

The setting in `deploy/openshift/01-config.yaml`:

```yaml
RDS_TLS_VERIFY: "true"
RDS_CA_FILE: "/etc/rds-ca/ca.crt"
```

tells the Python RouterOS client to verify the RDS HTTPS server certificate using that mounted CA certificate. The end-to-end flow is:

```text
./rds-rest-ca.crt on the administrator workstation
        |
        | oc create configmap
        v
rds-rest-ca ConfigMap
  key: ca.crt
        |
        | mounted by the CSI controller Deployment
        v
/etc/rds-ca/ca.crt inside the csi-driver container
        |
        | RDS_CA_FILE points to this file
        v
Python HTTPS certificate verification
        |
        v
RouterOS REST endpoint
```

For the validated lab configuration, keep both of these values:

```yaml
RDS_TLS_VERIFY: "true"
RDS_CA_FILE: "/etc/rds-ca/ca.crt"
```

and create the `rds-rest-ca` ConfigMap from the exported RouterOS CA before deploying the controller.

> **Important:** `rds-rest-ca` is marked `optional: true` in the controller Deployment so the same manifest can also be used when the RDS certificate is signed by a CA already trusted by the container image. However, if `RDS_CA_FILE` is set to `/etc/rds-ca/ca.crt` and the ConfigMap does not exist, that file will not exist in the container and REST TLS verification will fail.

If the RDS certificate chains to a CA already trusted by the container image, use:

```yaml
RDS_TLS_VERIFY: "true"
RDS_CA_FILE: ""
```

In that case the driver uses the container's normal system CA trust store, and the `rds-rest-ca` ConfigMap is not required.

For temporary lab troubleshooting only, `RDS_TLS_VERIFY=false` disables TLS certificate verification entirely. Do not use that setting for a normal deployment.

After the controller is deployed, you can confirm both the environment variable and mounted CA file with:

```bash
oc -n mikrotik-rds-csi exec \
  deploy/mikrotik-rds-csi-controller \
  -c csi-driver -- \
  python -c 'import os; from pathlib import Path; p=Path(os.environ.get("RDS_CA_FILE", "")); print("RDS_CA_FILE=", p); print("exists=", p.exists()); print("bytes=", p.stat().st_size if p.exists() else 0)'
```

For a private-CA deployment, the expected output should show `/etc/rds-ca/ca.crt`, `exists=True`, and a non-zero file size.

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

The project requires Python 3.10 or newer. On RHEL 9, the unversioned `python3` command normally points to Python 3.9, so **do not use `python3 -m venv` on a default RHEL 9 host** for this project. Use Python 3.11 explicitly (recommended and also used by the Containerfile), or another Python version >= 3.10.

Check the versions first:

```bash
python3 --version
python3.11 --version
```

On RHEL 9, install Python 3.11 and its pip package if they are not already present:

```bash
sudo dnf install -y python3.11 python3.11-pip
```

If `.venv` was previously created with the default RHEL 9 `python3`/Python 3.9, remove it and recreate it with Python 3.11:

```bash
deactivate 2>/dev/null || true
rm -rf .venv

python3.11 -m venv .venv
source .venv/bin/activate

python --version
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e '.[dev]'

make generate
make test
```

`make generate` invokes the helper through `bash`, so it does not depend on the executable bit being preserved when the repository is copied or extracted. The Containerfile uses the same approach during image builds.

The activated virtual environment should report Python 3.10 or newer; Python 3.11 is the recommended RHEL 9 development version. `grpcio==1.84.0` and `grpcio-tools==1.84.0` require Python 3.10 or newer. If pip reports that it can only find `grpcio` versions through `1.80.0`, check `python --version`: that symptom usually means the virtual environment was created with Python 3.9.

The container build performs protobuf generation automatically and uses Python 3.11, so this local virtual-environment setup is only required for running the CSI server/tests directly from the checkout.

## 5. Build and push the image

Quay image references use the registry form `quay.io/<namespace>/<repository>:<tag>`. Do **not** include `/repository/` in the image reference. Quay's web UI uses URLs such as `https://quay.io/repository/<namespace>/<repository>`, but `/repository/` is not part of the container image name.

For example, if the Quay namespace is `singlecheeze`, use:

```text
quay.io/singlecheeze/mikrotik-rds-csi:0.2.2
```

Authenticate to Quay before pushing:

```bash
podman login quay.io
```

Use your Quay username/password or a robot account/token that has write access to the repository. Then build and push:

```bash
IMAGE=quay.io/YOUR_USER/mikrotik-rds-csi:0.2.2
podman build -f Containerfile -t "${IMAGE}" .
podman push "${IMAGE}"
```

A successful build followed by an `authentication required` error during `podman push` normally means either:

- `podman login quay.io` has not been completed for the current user;
- the authenticated account does not have write access to the target namespace/repository; or
- the image was tagged with a Quay web-UI path such as `quay.io/repository/<namespace>/<repository>` instead of the registry path `quay.io/<namespace>/<repository>`.

If an image was already built with the incorrect `/repository/` path, it does not need to be rebuilt. Retag it and push the corrected name:

```bash
podman tag \
  quay.io/repository/YOUR_USER/mikrotik-rds-csi:0.2.2 \
  quay.io/YOUR_USER/mikrotik-rds-csi:0.2.2

IMAGE=quay.io/YOUR_USER/mikrotik-rds-csi:0.2.2
podman push "${IMAGE}"
```

If the repository does not yet exist, create `mikrotik-rds-csi` in the desired Quay namespace or use an account that is allowed to create repositories in that namespace.

Update `deploy/openshift/kustomization.yaml`:

```yaml
images:
  - name: quay.io/REPLACE_ME/mikrotik-rds-csi
    newName: quay.io/YOUR_USER/mikrotik-rds-csi
    newTag: 0.2.2
```

The base manifests intentionally keep the placeholder image:

```text
quay.io/REPLACE_ME/mikrotik-rds-csi:0.2.2
```

in both `03-controller.yaml` and `04-node.yaml`. Because deployment uses Kustomize (`oc apply -k`), you do **not** need to edit those two manifests separately. The `images` entry in `kustomization.yaml` rewrites the matching CSI driver image in both the controller Deployment and node DaemonSet.

If you choose to apply `03-controller.yaml` or `04-node.yaml` directly with `oc apply -f` instead of using Kustomize, then you must replace the placeholder image in each manifest manually.

Before deploying, render the manifests and verify that Kustomize replaced both CSI driver image references with the Quay image you pushed:

```bash
oc kustomize deploy/openshift | grep -E 'image:.*mikrotik-rds-csi'
```

For a `singlecheeze` repository, the rendered output should contain two CSI driver references similar to:

```text
image: quay.io/singlecheeze/mikrotik-rds-csi:0.2.2
image: quay.io/singlecheeze/mikrotik-rds-csi:0.2.2
```

One is the controller container from `03-controller.yaml`; the other is the node-plugin container from `04-node.yaml`.

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

### Raw block publish on RHCOS

The node plugin bind-publishes each NVMe namespace onto the exact kubelet CSI raw-block target file. The driver therefore checks that exact target with `findmnt --mountpoint`; it must not use `findmnt --target`, because `--target` returns the filesystem that merely contains the path (for example the RHCOS `/var/lib/kubelet` filesystem) and can incorrectly report that an unpublished target is already mounted.

If kubelet reports an error similar to:

```text
MapVolume.MapPodDevice failed ... target ... is already mounted from /dev/sda4[/ostree/deploy/rhcos/var/lib/kubelet]
```

use driver version `0.2.2` or newer. The PVC and RouterOS disk do not need to be recreated; updating the CSI node DaemonSet is sufficient, after which kubelet can retry the raw block publish.

## 8. Dynamic PVC and benchmark test

The benchmark workflow is now self-contained in `deploy/openshift/07-test-pod.yaml`. A single apply creates the `nvme-test` namespace if needed, dynamically provisions the disposable `rds-csi-test` raw-block PVC, creates the test ServiceAccount/RBAC, and starts the benchmark pod. You do **not** need to apply `06-test-pvc.yaml` first.

> **Warning:** this benchmark is destructive. The sequential and random write workloads overwrite the contents of the disposable `rds-csi-test` PVC. Do not change the manifest to point at a PVC containing data you need to preserve.

For the first run:

```bash
oc apply -f deploy/openshift/07-test-pod.yaml
oc -n nvme-test get pvc rds-csi-test -w
```

After the PVC binds, watch the benchmark pod and its logs:

```bash
oc -n nvme-test get pod rds-csi-test -w
oc -n nvme-test logs -f pod/rds-csi-test -c test
```

If you are using a deployment-specific manifest directory such as `deploy/openshift-lab-tailored`, apply the corresponding combined benchmark manifest instead:

```bash
oc apply -f deploy/openshift-lab-tailored/07-test-pod.yaml
```

`06-test-pvc.yaml` remains in the repository only as an optional PVC-only manifest for cases where you want to test dynamic provisioning independently of the benchmark pod.

### Rerunning the benchmark

The PVC can be reused for another destructive run. Delete only the completed test pod, then reapply the manifest:

```bash
oc -n nvme-test delete pod rds-csi-test --ignore-not-found
oc apply -f deploy/openshift/07-test-pod.yaml
oc -n nvme-test logs -f pod/rds-csi-test -c test
```

If you want each benchmark run to exercise **fresh dynamic provisioning**, delete both the pod and PVC before reapplying:

```bash
oc -n nvme-test delete pod rds-csi-test --ignore-not-found
oc -n nvme-test delete pvc rds-csi-test --ignore-not-found
oc apply -f deploy/openshift/07-test-pod.yaml
```

With the StorageClass `reclaimPolicy: Delete`, deleting this test PVC also exercises the CSI `DeleteVolume` path and removes the corresponding managed RouterOS file-backed disk.

### Benchmark container and kernel

The benchmark pod uses:

```text
quay.io/centos/centos:stream9
```

CentOS Stream 9 is used intentionally for the benchmark userspace because its standard repositories provide both `fio` and the split `fio-engine-libaio` package required by the test. The pod installs those packages quietly at startup and only prints the package-manager output if installation fails.

The earlier UBI 10 test image was removed because the enabled UBI 10 repositories do not contain `fio`. More importantly, the container image does **not** determine the NVMe/TCP kernel implementation: containers share the OpenShift node's RHCOS kernel. NVMe/TCP fixes and performance optimizations therefore come from the node kernel and its `nvme_tcp` module, regardless of whether the benchmark userspace is UBI or CentOS Stream.

The script uses `set -euo pipefail` rather than `set -x`, so the pod no longer prints a `+ <command>` trace for every shell command. Benchmark logs are intentionally compact: userspace version, node kernel, fio version, device size, and the four fio result sections.

A successful provision creates a RouterOS file disk whose path, NQN prefix, target address, target port, and NSID all come from the resolved ConfigMap/StorageClass configuration.

Controller logs:

```bash
oc -n mikrotik-rds-csi logs deploy/mikrotik-rds-csi-controller -c csi-driver -f
```

Provisioner logs:

```bash
oc -n mikrotik-rds-csi logs deploy/mikrotik-rds-csi-controller -c csi-provisioner -f
```

The benchmark runs four workloads automatically:

| Test | Workload | Block size | Queue depth | Duration |
|---|---|---:|---:|---:|
| Sequential write | `write` | 1 MiB | 32 | 30 seconds |
| Sequential read | `read` | 1 MiB | 32 | 30 seconds |
| Random write | `randwrite` | 4 KiB | 32 | 30 seconds |
| Random read | `randread` | 4 KiB | 32 | 30 seconds |

All four workloads use `direct=1` and the Linux `libaio` I/O engine. For the sequential tests, focus primarily on `BW`; for the random tests, `IOPS` and `clat`/latency percentiles are the most useful fields.

When the four tests finish, the pod remains running for inspection. The individual fio outputs are retained inside the pod as `/tmp/fio-seq-write.txt`, `/tmp/fio-seq-read.txt`, `/tmp/fio-rand-write.txt`, and `/tmp/fio-rand-read.txt`.

For example:

```bash
oc -n nvme-test exec rds-csi-test -c test -- cat /tmp/fio-rand-read.txt
```

## Delete behavior and safety

RouterOS deletes the `/disk` object separately from its file-backed image. The driver therefore records the actual `file-path` from the RouterOS disk before deleting the export, removes the disk object, then removes that exact backing file.

### RouterOS 32-character disk-slot limit

RouterOS limits a disk `slot` name to **32 characters**. New CSI volume IDs therefore use `csi-` plus the first **28 hexadecimal characters** of a SHA-256 digest, for exactly 32 characters total:

```text
csi-0123456789abcdef0123456789ab
```

That retains 112 bits of deterministic hash space while ensuring every dynamically provisioned RouterOS disk `slot` is exactly 32 characters long. The managed-volume guard accepts only this `csi-` plus 28-hex-character format, and `DeleteVolume` refuses IDs outside it.

This remains an experimental driver. Before production use, add CSI conformance/sanity testing, controller leader election/HA, stronger backend ownership metadata, failure-injection tests, node-reboot recovery tests, and snapshot/clone/expansion support as required.
