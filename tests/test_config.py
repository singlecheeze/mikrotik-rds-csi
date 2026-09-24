from __future__ import annotations

from mikrotik_rds_csi.config import ControllerConfig


def base_config() -> ControllerConfig:
    return ControllerConfig(
        api_endpoint="https://rds.example.test",
        username="user",
        password="pass",
        api_timeout_seconds=15.0,
        tls_verify=True,
        ca_file="/ca.crt",
        pool_slot="pool-a",
        pool_path="/pool-a",
        pool_filesystem="xfs",
        pool_required_state="clean",
        require_pool_mounted=True,
        storage_target="192.0.2.20",
        storage_port=4420,
        nqn_prefix="nqn.2026-09.example:rds",
        nsid=1,
        reserve_bytes=1024,
        file_extension=".img",
    )


def test_storageclass_parameters_override_environment_defaults():
    settings = base_config().volume_settings(
        {
            "poolSlot": "pool-b",
            "poolPath": "/pool-b/csi",
            "targetAddress": "198.51.100.20",
            "targetPort": "5520",
            "nqnPrefix": "nqn.2026-09.example:alternate",
            "nsid": "2",
            "reserveBytes": "2048",
            "fileExtension": "raw",
            "poolFilesystem": "",
            "poolRequiredState": "",
            "requirePoolMounted": "false",
        }
    )

    assert settings.pool_slot == "pool-b"
    assert settings.pool_path == "/pool-b/csi"
    assert settings.storage_target == "198.51.100.20"
    assert settings.storage_port == 5520
    assert settings.nqn_prefix == "nqn.2026-09.example:alternate"
    assert settings.nsid == 2
    assert settings.reserve_bytes == 2048
    assert settings.file_extension == ".raw"
    assert settings.pool_filesystem == ""
    assert settings.pool_required_state == ""
    assert settings.require_pool_mounted is False
    assert settings.file_path("csi-abc") == "/pool-b/csi/csi-abc.raw"


def test_requests_verify_uses_custom_ca_when_enabled():
    assert base_config().requests_verify == "/ca.crt"


def test_requests_verify_can_be_disabled():
    config = base_config().__class__(**{**base_config().__dict__, "tls_verify": False})
    assert config.requests_verify is False
