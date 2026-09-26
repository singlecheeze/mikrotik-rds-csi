from mikrotik_rds_csi.naming import (
    ROUTEROS_SLOT_MAX_LENGTH,
    is_managed_volume_id,
    nqn_for_volume,
    volume_id_from_name,
)


def test_volume_id_is_stable_and_exactly_32_characters():
    first = volume_id_from_name("pvc-1234")
    second = volume_id_from_name("pvc-1234")
    other = volume_id_from_name("pvc-5678")

    assert first == second
    assert first != other
    assert first.startswith("csi-")
    assert len(first) == ROUTEROS_SLOT_MAX_LENGTH == 32
    assert len(first.removeprefix("csi-")) == 28


def test_nqn_is_deterministic_and_normalizes_trailing_dot():
    assert nqn_for_volume("nqn.2026-09.example:rds.", "csi-abc") == (
        "nqn.2026-09.example:rds.csi-abc"
    )


def test_managed_volume_guard_accepts_only_32_character_csi_ids():
    assert is_managed_volume_id("csi-0123456789abcdef0123456789ab")

    assert not is_managed_volume_id("storage-pool")
    assert not is_managed_volume_id("csi-rest-test-001")
    assert not is_managed_volume_id("csi-0123456789abcdef0123456789abc")
    assert not is_managed_volume_id("csi-0123456789abcdef0123456789abcdef")
