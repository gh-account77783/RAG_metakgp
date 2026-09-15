"""Small JSON-over-HTTP transport shared by model adapters."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol


class HttpClientError(Exception):
    """Internal transport error that never includes response bodies or credentials."""


@dataclass(frozen=True, slots=True)
class HttpStatusError(HttpClientError):
    status: int

    def __str__(self) -> str:
        return f"HTTP request failed with status {self.status}"


class HttpTimeoutError(HttpClientError):
    pass


class HttpUnavailableError(HttpClientError):
    pass


class HttpPayloadError(HttpClientError):
    pass


class JsonTransport(Protocol):
    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any] | None,
        timeout: float,
    ) -> dict[str, Any]: ...


class UrllibJsonTransport:
    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any] | None,
        timeout: float,
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise HttpStatusError(int(exc.code)) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise HttpTimeoutError("HTTP request timed out") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise HttpUnavailableError("HTTP endpoint is unavailable") from exc
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpPayloadError("HTTP endpoint returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise HttpPayloadError("HTTP endpoint returned a non-object JSON payload")
        return decoded
