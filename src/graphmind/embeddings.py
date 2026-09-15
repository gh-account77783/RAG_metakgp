"""Embedding adapters with immutable index fingerprints."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Any, Protocol

from .domain import AdapterStatus
from .errors import (
    EmbeddingDimensionError,
    EmbeddingModelNotFoundError,
    EmbeddingResponseError,
    EmbeddingUnavailableError,
)
from .http_client import (
    HttpClientError,
    HttpStatusError,
    HttpTimeoutError,
    JsonTransport,
    UrllibJsonTransport,
)


class EmbeddingProvider(Protocol):
    dimension: int

    @property
    def fingerprint(self) -> str: ...

    def embed(self, text: str) -> list[float]: ...

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]: ...

    def readiness(self) -> AdapterStatus: ...


class HashEmbedding:
    """Deterministic offline diagnostic embedding; not a production semantic model."""

    algorithm = "graphmind-hash-embedding-v1"

    def __init__(self, dimension: int = 384) -> None:
        if dimension < 32:
            raise ValueError("Embedding dimension must be at least 32")
        self.dimension = dimension

    @property
    def fingerprint(self) -> str:
        return f"{self.algorithm}:{self.dimension}"

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        tokens = re.findall(r"[\w'-]+", text.casefold(), flags=re.UNICODE)
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=16).digest()
            first = int.from_bytes(digest[:8], "big")
            second = int.from_bytes(digest[8:], "big")
            vector[first % self.dimension] += 1.0 if second & 1 else -1.0
        magnitude = math.sqrt(sum(value * value for value in vector))
        return vector if magnitude == 0 else [value / magnitude for value in vector]

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed(text) for text in texts]

    def readiness(self) -> AdapterStatus:
        return AdapterStatus("hash-embedding", True, "diagnostic-only")


def _precision_family(value: str) -> str:
    normalized = value.casefold().replace("-", "").replace("_", "")
    if normalized in {"f16", "fp16", "float16"}:
        return "fp16"
    if normalized in {"f8", "fp8", "float8"}:
        return "fp8"
    if normalized in {"q80", "int8", "i8"}:
        return "int8"
    return value.casefold()


class OllamaEmbedding:
    """Ollama `/api/embed` adapter used for the BGE-M3 P4 baseline."""

    def __init__(
        self,
        base_url: str,
        model: str,
        model_id: str,
        *,
        dimension: int = 1024,
        revision: str = "auto",
        precision: str = "auto",
        api_key: str = "",
        timeout: float = 60.0,
        batch_size: int = 16,
        transport: JsonTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.model_id = model_id
        self.dimension = dimension
        self.configured_revision = revision
        self.configured_precision = precision
        self.api_key = api_key
        self.timeout = timeout
        self.batch_size = batch_size
        self.transport = transport or UrllibJsonTransport()
        self._identity: tuple[str, str] | None = None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    @staticmethod
    def _matches_model(candidate: str, selected: str) -> bool:
        return (
            candidate == selected
            or candidate == f"{selected}:latest"
            or f"{candidate}:latest" == selected
        )

    def _resolve_identity(self) -> tuple[str, str]:
        if self._identity is not None:
            return self._identity
        if self.configured_revision != "auto":
            self._identity = (self.configured_revision, self.configured_precision)
            return self._identity
        try:
            payload = self.transport.request_json(
                "GET",
                f"{self.base_url}/api/tags",
                headers=self._headers(),
                payload=None,
                timeout=self.timeout,
            )
        except HttpStatusError as exc:
            if exc.status == 404:
                raise EmbeddingModelNotFoundError("Embedding model is not installed") from exc
            raise EmbeddingUnavailableError("Embedding runtime is unavailable") from exc
        except HttpClientError as exc:
            raise EmbeddingUnavailableError("Embedding runtime is unavailable") from exc
        models = payload.get("models")
        if not isinstance(models, list):
            raise EmbeddingResponseError("Embedding runtime returned invalid model metadata")
        selected: dict[str, Any] | None = None
        for item in models:
            if not isinstance(item, dict):
                continue
            names = (str(item.get("name", "")), str(item.get("model", "")))
            if any(self._matches_model(name, self.model) for name in names):
                selected = item
                break
        if selected is None:
            raise EmbeddingModelNotFoundError(f"Embedding model is not installed: {self.model}")
        digest = str(selected.get("digest", "")).strip()
        details = selected.get("details") if isinstance(selected.get("details"), dict) else {}
        actual_precision = str(details.get("quantization_level", "unknown")).strip() or "unknown"
        if len(digest) != 64 or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise EmbeddingResponseError("Embedding model metadata is missing a full digest")
        if self.configured_precision != "auto" and _precision_family(
            self.configured_precision
        ) != _precision_family(actual_precision):
            raise EmbeddingResponseError(
                "Embedding precision does not match the configured precision"
            )
        self._identity = (digest.casefold(), actual_precision)
        return self._identity

    @property
    def fingerprint(self) -> str:
        revision, precision = self._resolve_identity()
        return (
            f"ollama:{self.model_id}@{revision}:precision={_precision_family(precision)}"
            f":dim={self.dimension}"
        )

    def identity(self) -> dict[str, object]:
        revision, precision = self._resolve_identity()
        return {
            "provider": "ollama",
            "base_url": self.base_url,
            "model": self.model,
            "model_id": self.model_id,
            "revision": revision,
            "precision": precision,
            "dimension": self.dimension,
            "timeout_seconds": self.timeout,
            "batch_size": self.batch_size,
            "fingerprint": self.fingerprint,
        }

    def _validate_vectors(self, payload: dict[str, Any], expected: int) -> list[list[float]]:
        raw_vectors = payload.get("embeddings")
        if not isinstance(raw_vectors, list) or len(raw_vectors) != expected:
            raise EmbeddingResponseError("Embedding runtime returned the wrong vector count")
        vectors: list[list[float]] = []
        for raw in raw_vectors:
            if not isinstance(raw, list) or len(raw) != self.dimension:
                raise EmbeddingDimensionError(
                    f"Embedding runtime returned a vector with dimension other than {self.dimension}"
                )
            try:
                vector = [float(value) for value in raw]
            except (TypeError, ValueError) as exc:
                raise EmbeddingResponseError("Embedding vector contains non-numeric values") from exc
            if not all(math.isfinite(value) for value in vector):
                raise EmbeddingResponseError("Embedding vector contains a non-finite value")
            vectors.append(vector)
        return vectors

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        result: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = list(texts[start : start + self.batch_size])
            try:
                payload = self.transport.request_json(
                    "POST",
                    f"{self.base_url}/api/embed",
                    headers=self._headers(),
                    payload={"model": self.model, "input": batch, "truncate": True},
                    timeout=self.timeout,
                )
            except HttpStatusError as exc:
                if exc.status == 404:
                    raise EmbeddingModelNotFoundError(
                        f"Embedding model is not installed: {self.model}"
                    ) from exc
                raise EmbeddingUnavailableError("Embedding runtime is unavailable") from exc
            except HttpTimeoutError as exc:
                raise EmbeddingUnavailableError("Embedding request timed out") from exc
            except HttpClientError as exc:
                raise EmbeddingUnavailableError("Embedding runtime is unavailable") from exc
            result.extend(self._validate_vectors(payload, len(batch)))
        return result

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def readiness(self) -> AdapterStatus:
        try:
            identity = self.identity()
            return AdapterStatus(
                "embedding",
                True,
                f"{identity['model_id']} {identity['precision']} dim={identity['dimension']}",
            )
        except (EmbeddingUnavailableError, EmbeddingModelNotFoundError, EmbeddingResponseError) as exc:
            return AdapterStatus("embedding", False, str(exc))


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise EmbeddingDimensionError("Cannot compare embeddings with different dimensions")
    return sum(a * b for a, b in zip(left, right, strict=True))
