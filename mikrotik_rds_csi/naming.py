from __future__ import annotations

import hashlib
import re


def volume_id_from_name(name: str) -> str:
    """Return a stable RouterOS-safe volume ID for a CSI CreateVolume name."""
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]
    return f"csi-{digest}"


_MANAGED_VOLUME_RE = re.compile(r"^csi-[0-9a-f]{32}$")


def is_managed_volume_id(volume_id: str) -> bool:
    return bool(_MANAGED_VOLUME_RE.fullmatch(volume_id))


def nqn_for_volume(prefix: str, volume_id: str) -> str:
    return f"{prefix.rstrip('.')}.{volume_id}"
