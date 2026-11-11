from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import requests


class KubernetesAPIError(RuntimeError):
    pass


class InClusterKubeClient:
    def __init__(
        self,
        *,
        api_url: str | None = None,
        token_file: str = "/var/run/secrets/kubernetes.io/serviceaccount/token",
        ca_file: str = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
        timeout: float = 10.0,
    ) -> None:
        host = os.getenv("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
        port = os.getenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        self.api_url = (api_url or f"https://{host}:{port}").rstrip("/")
        self.token_file = token_file
        self.ca_file = ca_file
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = ca_file if Path(ca_file).exists() else True

    def _service_account_token(self) -> str:
        try:
            return Path(self.token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise KubernetesAPIError(f"unable to read Kubernetes service account token: {exc}") from exc

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        expected: tuple[int, ...] = (200,),
        **kwargs: Any,
    ) -> Any:
        url = f"{self.api_url}/{path.lstrip('/')}"
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Accept", "application/json")
        headers["Authorization"] = f"Bearer {token or self._service_account_token()}"

        try:
            response = self.session.request(
                method,
                url,
                headers=headers,
                timeout=self.timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise KubernetesAPIError(f"Kubernetes API {method} {path} failed: {exc}") from exc

        if response.status_code not in expected:
            body = response.text.strip()
            if len(body) > 800:
                body = body[:800] + "..."
            raise KubernetesAPIError(
                f"Kubernetes API {method} {path} returned {response.status_code}: {body}"
            )

        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise KubernetesAPIError(
                f"Kubernetes API {method} {path} returned non-JSON data"
            ) from exc

    def get(self, path: str, *, token: str | None = None, **kwargs: Any) -> Any:
        return self.request("GET", path, token=token, **kwargs)

    def post(self, path: str, *, token: str | None = None, **kwargs: Any) -> Any:
        return self.request("POST", path, token=token, **kwargs)

    def put(self, path: str, *, token: str | None = None, **kwargs: Any) -> Any:
        return self.request("PUT", path, token=token, **kwargs)

    def self_subject_access_review(
        self,
        token: str,
        *,
        verb: str,
        resource: str,
        group: str = "",
        namespace: str | None = None,
    ) -> bool:
        attributes: dict[str, Any] = {
            "verb": verb,
            "resource": resource,
            "group": group,
        }
        if namespace:
            attributes["namespace"] = namespace
        payload = {
            "apiVersion": "authorization.k8s.io/v1",
            "kind": "SelfSubjectAccessReview",
            "spec": {"resourceAttributes": attributes},
        }
        data = self.post(
            "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews",
            token=token,
            json=payload,
            expected=(201,),
        )
        return bool((data or {}).get("status", {}).get("allowed", False))
