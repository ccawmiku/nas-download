from __future__ import annotations

import http.client
import json
import os
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ServiceDefinition:
    key: str
    name: str
    port: int
    path: str
    config: str
    host: str = "127.0.0.1"


class ServiceAdapter:
    """Small HTTP boundary around one platform worker.

    The unified console only knows this contract. A worker can be replaced,
    upgraded, or moved behind another process without changing the dashboard.
    """

    def __init__(self, definition: ServiceDefinition, host: str | None = None) -> None:
        self.definition = definition
        self.host = host or definition.host

    @property
    def key(self) -> str:
        return self.definition.key

    def request(self, method: str, path: str, body: bytes | None = None, timeout: float = 10, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection(self.host, self.definition.port, timeout=timeout)
        try:
            request_headers = {"Content-Type": "application/json"} if body is not None else {}
            request_headers.update(headers or {})
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def ready(self, timeout: float = 1.5) -> bool:
        try:
            status, _ = self.request("GET", "/api/status", timeout=timeout)
            return status == 200
        except (OSError, TimeoutError, http.client.HTTPException):
            return False

    def status(self) -> dict[str, Any]:
        try:
            status, body = self.request("GET", "/api/status", timeout=1.5)
            if status != 200:
                return {}
            payload = json.loads(body.decode("utf-8", errors="replace") or "{}")
            return payload if isinstance(payload, dict) else {}
        except (OSError, TimeoutError, http.client.HTTPException, json.JSONDecodeError):
            return {}

    def run_now(self) -> dict[str, Any]:
        try:
            status, body = self.request("POST", "/api/run-now", body=b"{}", timeout=10)
            raw = body.decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw or "{}")
            except json.JSONDecodeError:
                payload = {"body": raw[:500]}
            if not isinstance(payload, dict):
                payload = {"body": raw[:500]}
            payload.setdefault("ok", 200 <= status < 300)
            payload.setdefault("status", status)
            return payload
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            return {"ok": False, "status": 0, "error": str(error)}


class TelegramAdapter(ServiceAdapter):
    def status(self) -> dict[str, Any]:
        token = os.getenv("INTERNAL_API_TOKEN", "")
        if not token:
            return {}
        try:
            status, body = self.request("GET", "/internal/status", timeout=1.5,
                                        headers={"X-NAS-Download-Token": token})
            payload = json.loads(body) if status == 200 else {}
            return payload if isinstance(payload, dict) else {}
        except (OSError, TimeoutError, http.client.HTTPException, json.JSONDecodeError):
            return {}

    def run_now(self) -> dict[str, Any]:
        return {"ok": False, "status": 405, "error": "Telegram 通过媒体消息入队"}


class ServiceAdapterRegistry:
    def __init__(self, definitions: Mapping[str, Mapping[str, Any]]) -> None:
        self._adapters = {
            key: (TelegramAdapter if key == "telegram" else ServiceAdapter)(
                ServiceDefinition(
                    key=key,
                    name=str(item["name"]),
                    port=int(item["port"]),
                    path=str(item["path"]),
                    config=str(item.get("config", "")),
                    host=str(item.get("host", "127.0.0.1")),
                )
            )
            for key, item in definitions.items()
        }

    def __getitem__(self, key: str) -> ServiceAdapter:
        return self._adapters[key]

    def values(self):
        return self._adapters.values()
