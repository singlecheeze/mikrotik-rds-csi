from __future__ import annotations

from dataclasses import dataclass
import os


TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"required environment variable {name} is not set")
    return value or ""


def _env_alias(primary: str, legacy: str, default: str = "") -> str:
    value = os.getenv(primary)
    if value is not None:
        return value
    value = os.getenv(legacy)
    if value is not None:
        return value
    return default


def _parse_bool(value: str, *, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise RuntimeError(f"{name} must be one of true/false, yes/no, on/off, 1/0; got {value!r}")


def _parse_int(value: str, *, name: str, minimum: int | None = None) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer; got {value!r}") from exc
    if minimum is not None and parsed < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}; got {parsed}")
    return parsed


def _clean_path(value: str) -> str:
    value = value.strip()
    if value == "/":
        return value
    return value.rstrip("/")


def _clean_extension(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if "/" in value or "\\" in value:
        raise RuntimeError("fileExtension/RDS_FILE_EXTENSION must not contain a path separator")
    return value if value.startswith(".") else f".{value}"


def _parameter(parameters: dict[str, str], name: str, default: str) -> str:
    value = parameters.get(name)
    return default if value is None else str(value)


@dataclass(frozen=True)
class VolumeSettings:
    pool_slot: str
    pool_path: str
    pool_filesystem: str
    pool_required_state: str
    require_pool_mounted: bool
    storage_target: str
    storage_port: int
    nqn_prefix: str
    nsid: int
    reserve_bytes: int
    file_extension: str

    def file_path(self, volume_id: str) -> str:
        name = f"{volume_id}{self.file_extension}"
        if self.pool_path == "/":
            return f"/{name}"
        return f"{self.pool_path}/{name}"


@dataclass(frozen=True)
class ControllerConfig:
    api_endpoint: str
    username: str
    password: str
    api_timeout_seconds: float
    tls_verify: bool
    ca_file: str

    # Global defaults. Each of these may be overridden by StorageClass
    # parameters on a per-class basis.
    pool_slot: str
    pool_path: str
    pool_filesystem: str
    pool_required_state: str
    require_pool_mounted: bool
    storage_target: str
    storage_port: int
    nqn_prefix: str
    nsid: int
    reserve_bytes: int
    file_extension: str

    @classmethod
    def from_env(cls) -> "ControllerConfig":
        api_timeout = float(_env("RDS_API_TIMEOUT_SECONDS", "15"))
        if api_timeout <= 0:
            raise RuntimeError("RDS_API_TIMEOUT_SECONDS must be > 0")

        return cls(
            api_endpoint=_env("RDS_API_ENDPOINT", required=True).rstrip("/"),
            username=_env("RDS_USERNAME", required=True),
            password=_env("RDS_PASSWORD", required=True),
            api_timeout_seconds=api_timeout,
            tls_verify=_parse_bool(_env("RDS_TLS_VERIFY", "true"), name="RDS_TLS_VERIFY"),
            ca_file=_env("RDS_CA_FILE", ""),
            pool_slot=_env("RDS_POOL_SLOT", ""),
            pool_path=_clean_path(_env("RDS_POOL_PATH", "")),
            pool_filesystem=_env("RDS_POOL_FILESYSTEM", "xfs").strip(),
            pool_required_state=_env("RDS_POOL_REQUIRED_STATE", "clean").strip(),
            require_pool_mounted=_parse_bool(
                _env("RDS_POOL_REQUIRE_MOUNTED", "true"), name="RDS_POOL_REQUIRE_MOUNTED"
            ),
            storage_target=_env_alias("RDS_NVME_TARGET_ADDRESS", "RDS_STORAGE_TARGET", "").strip(),
            storage_port=_parse_int(
                _env_alias("RDS_NVME_TARGET_PORT", "RDS_STORAGE_PORT", "4420"),
                name="RDS_NVME_TARGET_PORT",
                minimum=1,
            ),
            nqn_prefix=_env("RDS_NQN_PREFIX", "").strip().rstrip("."),
            nsid=_parse_int(_env("RDS_NVME_NSID", "1"), name="RDS_NVME_NSID", minimum=1),
            reserve_bytes=_parse_int(
                _env("RDS_RESERVE_BYTES", str(1024**3)), name="RDS_RESERVE_BYTES", minimum=0
            ),
            file_extension=_clean_extension(_env("RDS_FILE_EXTENSION", ".img")),
        )

    @property
    def requests_verify(self) -> bool | str:
        if not self.tls_verify:
            return False
        return self.ca_file or True

    def volume_settings(self, parameters: dict[str, str] | None = None) -> VolumeSettings:
        parameters = parameters or {}

        pool_slot = _parameter(parameters, "poolSlot", self.pool_slot).strip()
        pool_path = _clean_path(_parameter(parameters, "poolPath", self.pool_path))
        storage_target = _parameter(parameters, "targetAddress", self.storage_target).strip()
        nqn_prefix = _parameter(parameters, "nqnPrefix", self.nqn_prefix).strip().rstrip(".")

        missing = [
            name
            for name, value in (
                ("poolSlot", pool_slot),
                ("poolPath", pool_path),
                ("targetAddress", storage_target),
                ("nqnPrefix", nqn_prefix),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "missing required RDS volume configuration: "
                + ", ".join(missing)
                + "; set the corresponding environment default or StorageClass parameter"
            )

        return VolumeSettings(
            pool_slot=pool_slot,
            pool_path=pool_path,
            pool_filesystem=_parameter(parameters, "poolFilesystem", self.pool_filesystem).strip(),
            pool_required_state=_parameter(
                parameters, "poolRequiredState", self.pool_required_state
            ).strip(),
            require_pool_mounted=_parse_bool(
                _parameter(
                    parameters,
                    "requirePoolMounted",
                    "true" if self.require_pool_mounted else "false",
                ),
                name="requirePoolMounted",
            ),
            storage_target=storage_target,
            storage_port=_parse_int(
                _parameter(parameters, "targetPort", str(self.storage_port)),
                name="targetPort",
                minimum=1,
            ),
            nqn_prefix=nqn_prefix,
            nsid=_parse_int(
                _parameter(parameters, "nsid", str(self.nsid)), name="nsid", minimum=1
            ),
            reserve_bytes=_parse_int(
                _parameter(parameters, "reserveBytes", str(self.reserve_bytes)),
                name="reserveBytes",
                minimum=0,
            ),
            file_extension=_clean_extension(
                _parameter(parameters, "fileExtension", self.file_extension)
            ),
        )


@dataclass(frozen=True)
class NodeConfig:
    node_id: str
    state_dir: str
    connect_timeout_seconds: int
    storage_interface: str
    use_route_source_address: bool
    reconnect_delay_seconds: int
    ctrl_loss_tmo_seconds: int
    nvme_module: str

    @classmethod
    def from_env(cls) -> "NodeConfig":
        return cls(
            node_id=_env("NODE_ID", required=True),
            state_dir=_env("RDS_NODE_STATE_DIR", "/var/lib/mikrotik-rds-csi"),
            connect_timeout_seconds=_parse_int(
                _env("RDS_CONNECT_TIMEOUT_SECONDS", "20"),
                name="RDS_CONNECT_TIMEOUT_SECONDS",
                minimum=1,
            ),
            # Empty means "do not enforce a specific interface". The node
            # driver still uses the kernel route to the per-volume target.
            storage_interface=_env("RDS_STORAGE_INTERFACE", "").strip(),
            use_route_source_address=_parse_bool(
                _env("RDS_USE_ROUTE_SOURCE_ADDRESS", "true"),
                name="RDS_USE_ROUTE_SOURCE_ADDRESS",
            ),
            reconnect_delay_seconds=_parse_int(
                _env("RDS_NVME_RECONNECT_DELAY_SECONDS", "10"),
                name="RDS_NVME_RECONNECT_DELAY_SECONDS",
                minimum=0,
            ),
            ctrl_loss_tmo_seconds=_parse_int(
                _env("RDS_NVME_CTRL_LOSS_TMO_SECONDS", "600"),
                name="RDS_NVME_CTRL_LOSS_TMO_SECONDS",
            ),
            nvme_module=_env("RDS_NVME_MODULE", "nvme_tcp").strip() or "nvme_tcp",
        )
