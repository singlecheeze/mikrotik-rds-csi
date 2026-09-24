from __future__ import annotations

from dataclasses import dataclass

from mikrotik_rds_csi.routeros import RouterOSClient


@dataclass
class FakeResponse:
    payload: object = None
    content: bytes = b"{}"

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.auth = None
        self.verify = None
        self.headers = {}
        self.calls = []
        self.responses = []

    def request(self, method, url, timeout=None, **kwargs):
        self.calls.append((method, url, timeout, kwargs))
        response = self.responses.pop(0)
        if response.payload is None:
            response.content = b""
        return response


def client_with_session():
    client = RouterOSClient("https://rds.example.test", "user", "pass", "/ca.crt")
    session = FakeSession()
    client.session = session
    return client, session


def test_create_file_disk_uses_exact_bytes():
    client, session = client_with_session()
    session.responses.append(FakeResponse({".id": "*2B", "slot": "csi-test"}))

    result = client.create_file_disk("csi-test", "/pool-a/csi-test.raw", 1073741824)

    assert result[".id"] == "*2B"
    method, url, _, kwargs = session.calls[0]
    assert method == "PUT"
    assert url.endswith("/rest/disk")
    assert kwargs["json"]["file-size"] == "1073741824"
    assert kwargs["json"]["file-path"] == "/pool-a/csi-test.raw"
    assert kwargs["json"]["mount-filesystem"] == "false"


def test_enable_export_sets_nqn_and_port():
    client, session = client_with_session()
    session.responses.append(FakeResponse({".id": "*2B", "nvme-tcp-export": "true"}))

    client.enable_nvme_export("*2B", "nqn.example:rds.csi-test", 4421)

    method, url, _, kwargs = session.calls[0]
    assert method == "PATCH"
    assert url.endswith("/rest/disk/*2B")
    assert kwargs["json"]["nvme-tcp-export"] == "true"
    assert kwargs["json"]["nvme-tcp-server-nqn"] == "nqn.example:rds.csi-test"
    assert kwargs["json"]["nvme-tcp-server-port"] == "4421"


def test_file_lookup_normalizes_leading_slash():
    client, session = client_with_session()
    session.responses.append(
        FakeResponse([{".id": "**abc", "name": "pool-a/csi-test.raw", "size": "1024"}])
    )

    result = client.get_file_by_name("/pool-a/csi-test.raw")
    assert result[".id"] == "**abc"


def test_find_files_for_volume_id_is_path_and_extension_agnostic():
    client, session = client_with_session()
    volume_id = "csi-0123456789abcdef0123456789abcdef"
    session.responses.append(
        FakeResponse(
            [
                {".id": "**a", "name": f"pool-a/{volume_id}.img"},
                {".id": "**b", "name": f"other/{volume_id}.raw"},
                {".id": "**c", "name": f"pool-a/not-{volume_id}.img"},
            ]
        )
    )

    result = client.find_files_for_volume_id(volume_id)
    assert [item[".id"] for item in result] == ["**a", "**b"]
