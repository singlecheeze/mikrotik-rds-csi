from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests


class RouterOSError(RuntimeError):
    pass


@dataclass(frozen=True)
class PoolStatus:
    slot: str
    mounted: bool
    state: str
    filesystem: str
    free_bytes: int


class RouterOSClient:
    def __init__(
        self,
        endpoint: str,
        username: str,
        password: str,
        verify: bool | str = True,
        timeout: float = 15.0,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.auth = (username, password)
        self.session.verify = verify
        self.session.headers.update({"Accept": "application/json"})

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.endpoint}/rest/{path.lstrip('/')}"
        try:
            response = self.session.request(method, url, timeout=self.timeout, **kwargs)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RouterOSError(f"RouterOS REST {method} {path} failed: {exc}") from exc

        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise RouterOSError(f"RouterOS REST {method} {path} returned non-JSON data") from exc

    def list_disks(self) -> list[dict[str, Any]]:
        data = self._request("GET", "disk")
        return data if isinstance(data, list) else []

    def get_disk_by_slot(self, slot: str) -> dict[str, Any] | None:
        for disk in self.list_disks():
            if disk.get("slot") == slot:
                return disk
        return None

    def get_pool_status(self, slot: str) -> PoolStatus:
        disk = self.get_disk_by_slot(slot)
        if not disk:
            raise RouterOSError(f"pool slot {slot!r} was not found")
        return PoolStatus(
            slot=slot,
            mounted=str(disk.get("mounted", "false")).lower() == "true",
            state=str(disk.get("state", "")),
            filesystem=str(disk.get("fs", "")),
            free_bytes=int(disk.get("free", 0) or 0),
        )

    def create_file_disk(self, slot: str, file_path: str, size_bytes: int) -> dict[str, Any]:
        payload = {
            "type": "file",
            "file-path": file_path,
            "file-size": str(size_bytes),
            "slot": slot,
            "mount-filesystem": "false",
        }
        data = self._request("PUT", "disk", json=payload)
        if not isinstance(data, dict) or ".id" not in data:
            raise RouterOSError("RouterOS did not return a disk object after create")
        return data

    def update_disk(self, disk_id: str, **properties: str) -> dict[str, Any]:
        encoded = quote(disk_id, safe="*")
        data = self._request("PATCH", f"disk/{encoded}", json=properties)
        if not isinstance(data, dict):
            raise RouterOSError(f"RouterOS did not return disk {disk_id} after update")
        return data

    def enable_nvme_export(self, disk_id: str, nqn: str, port: int) -> dict[str, Any]:
        return self.update_disk(
            disk_id,
            **{
                "nvme-tcp-export": "true",
                "nvme-tcp-server-port": str(port),
                "nvme-tcp-server-nqn": nqn,
                "mount-filesystem": "false",
            },
        )

    def disable_nvme_export(self, disk_id: str) -> dict[str, Any]:
        return self.update_disk(disk_id, **{"nvme-tcp-export": "false"})

    def delete_disk(self, disk_id: str) -> None:
        encoded = quote(disk_id, safe="*")
        self._request("DELETE", f"disk/{encoded}")

    def list_files(self) -> list[dict[str, Any]]:
        data = self._request("GET", "file")
        return data if isinstance(data, list) else []

    def get_file_by_name(self, name: str) -> dict[str, Any] | None:
        normalized = name.lstrip("/")
        for item in self.list_files():
            if str(item.get("name", "")).lstrip("/") == normalized:
                return item
        return None


    def find_files_for_volume_id(self, volume_id: str) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        for item in self.list_files():
            name = str(item.get("name", "")).rstrip("/")
            base = name.rsplit("/", 1)[-1]
            if base == volume_id or base.startswith(f"{volume_id}."):
                matches.append(item)
        return matches

    def delete_file(self, file_id: str) -> None:
        encoded = quote(file_id, safe="*")
        self._request("DELETE", f"file/{encoded}")
