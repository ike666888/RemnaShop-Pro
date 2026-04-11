import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_OPENAPI_PATH = Path("docs/remnawave-openapi.json")


@dataclass
class OperationSpec:
    operation_id: str
    method: str
    path: str
    tags: list[str]
    parameters: list[dict[str, Any]]
    has_json_body: bool


class RemnawaveApiClient:
    """
    OpenAPI-driven Remnawave API client.
    - Source of truth: docs/remnawave-openapi.json
    - No hardcoded endpoint payload fields.
    """

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        spec_path: str | Path = DEFAULT_OPENAPI_PATH,
        spec_data: dict[str, Any] | None = None,
        timeout: float = 20.0,
        verify_tls: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token or ""
        self.spec_path = Path(spec_path)
        self.spec_data = spec_data
        self.timeout = timeout
        self.verify_tls = verify_tls
        self.spec = self._load_spec()
        self.operations = self._build_operations()
        try:
            import httpx  # lazy import for environments without deps during static/unit checks
            self._httpx = httpx
            self._client = httpx.AsyncClient(timeout=self.timeout, verify=self.verify_tls)
        except Exception:
            self._httpx = None
            self._client = None

    def _load_spec(self) -> dict[str, Any]:
        if isinstance(self.spec_data, dict):
            return self.spec_data
        env_path = os.getenv("REMNAWAVE_OPENAPI_PATH", "").strip()
        candidates = [Path(env_path)] if env_path else []
        candidates.extend([self.spec_path, Path("docs/openapi.json")])
        real_path = next((p for p in candidates if p and p.exists()), None)
        if real_path is None:
            hint = ", ".join(str(p) for p in candidates if p)
            raise FileNotFoundError(f"OpenAPI spec not found. Tried: {hint}")
        with real_path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def _build_operations(self) -> dict[str, OperationSpec]:
        paths = self.spec.get("paths", {})
        ops: dict[str, OperationSpec] = {}
        for path, methods in paths.items():
            if not isinstance(methods, dict):
                continue
            for method, content in methods.items():
                if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                    continue
                if not isinstance(content, dict):
                    continue
                operation_id = content.get("operationId")
                if not operation_id:
                    continue
                request_body = content.get("requestBody") or {}
                body_content = request_body.get("content") if isinstance(request_body, dict) else {}
                has_json_body = isinstance(body_content, dict) and "application/json" in body_content
                ops[operation_id] = OperationSpec(
                    operation_id=operation_id,
                    method=method.upper(),
                    path=path,
                    tags=[str(t) for t in (content.get("tags") or [])],
                    parameters=[p for p in (content.get("parameters") or []) if isinstance(p, dict)],
                    has_json_body=has_json_body,
                )
        return ops

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def get_operations_by_tag(self, tag_keyword: str) -> list[OperationSpec]:
        key = tag_keyword.lower()
        return [
            op
            for op in self.operations.values()
            if any(key in tag.lower() for tag in op.tags)
        ]

    def get_auth_operations(self) -> list[OperationSpec]:
        return self.get_operations_by_tag("auth")

    def get_user_operations(self) -> list[OperationSpec]:
        candidates: list[OperationSpec] = []
        for op in self.operations.values():
            tags = [t.lower() for t in op.tags]
            if any("user" in t for t in tags):
                candidates.append(op)
                continue
            if "/users" in op.path:
                candidates.append(op)
        return candidates

    def _build_url(self, path: str, path_params: dict[str, Any] | None = None) -> str:
        resolved = path
        for key, value in (path_params or {}).items():
            resolved = resolved.replace("{" + str(key) + "}", str(value))
        return f"{self.base_url}{resolved}"

    async def call_operation(
        self,
        operation_id: str,
        *,
        path_params: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        op = self.operations.get(operation_id)
        if not op:
            raise ValueError(f"Unknown operationId: {operation_id}")
        if self._client is None:
            raise RuntimeError("httpx is required to perform HTTP calls")
        url = self._build_url(op.path, path_params=path_params)
        kwargs: dict[str, Any] = {"headers": self._headers()}
        if query:
            kwargs["params"] = query
        if json_body is not None:
            kwargs["json"] = json_body
        return await self._client.request(op.method, url, **kwargs)

    async def call_auth_operation(self, operation_id: str, **kwargs) -> Any:
        op = self.operations.get(operation_id)
        if not op:
            raise ValueError(f"Unknown operationId: {operation_id}")
        if not any("auth" in tag.lower() for tag in op.tags):
            raise ValueError(f"Operation is not auth-tagged: {operation_id}")
        return await self.call_operation(operation_id, **kwargs)

    async def call_user_operation(self, operation_id: str, **kwargs) -> Any:
        op = self.operations.get(operation_id)
        if not op:
            raise ValueError(f"Unknown operationId: {operation_id}")
        is_user_tagged = any("user" in tag.lower() for tag in op.tags)
        is_user_path = "/users" in op.path
        if not (is_user_tagged or is_user_path):
            raise ValueError(f"Operation is not user-tagged/path: {operation_id}")
        return await self.call_operation(operation_id, **kwargs)
