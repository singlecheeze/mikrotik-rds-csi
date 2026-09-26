from __future__ import annotations

import hashlib
import re


# RouterOS disk slot names are limited to 32 characters. Keep the human-readable
# CSI prefix and use 28 hexadecimal SHA-256 characters (112 bits) so every CSI
# volume ID / RouterOS slot name is exactly 32 characters long.
ROUTEROS_SLOT_MAX_LENGTH = 32
_VOLUME_ID_PREFIX = "csi-"
_VOLUME_HASH_HEX_LENGTH = ROUTEROS_SLOT_MAX_LENGTH - len(_VOLUME_ID_PREFIX)


def volume_id_from_name(name: str) -> str:
    """Return a stable 32-character RouterOS-safe volume ID."""
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:_VOLUME_HASH_HEX_LENGTH]
    return f"{_VOLUME_ID_PREFIX}{digest}"


_MANAGED_VOLUME_RE = re.compile(r"^csi-[0-9a-f]{28}$")


def is_managed_volume_id(volume_id: str) -> bool:
    return bool(_MANAGED_VOLUME_RE.fullmatch(volume_id))


def nqn_for_volume(prefix: str, volume_id: str) -> str:
    return f"{prefix.rstrip('.')}.{volume_id}"
