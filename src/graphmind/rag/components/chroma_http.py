"""Pinned Chroma server adapter with bounded requests, including construction."""
from chromadb.api.fastapi import FastAPI
from chromadb.api.models.Collection import Collection
from chromadb.config import Settings, System
from importlib.metadata import version
from typing import Any, override

from ...errors import ConfigurationError, DependencyUnavailableError


class BoundedChromaHTTP(FastAPI):
    def __init__(self, system):
        if version("chromadb") != "1.5.9":
            raise DependencyUnavailableError("HTTP storage requires the qualified Chroma 1.5.9 profile")
        super().__init__(system)

    # Chroma 1.5.9 creates its HTTP session with timeout=None. Override only
    # request dispatch; retain the upstream protocol, serialization and errors.
    @override
    def _make_request(self, method: str, path: str, **kwargs: Any) -> Any:
        kwargs["timeout"] = 30.0
        return super()._make_request(method, path, **kwargs)


class ChromaHTTPClient:
    """Use the pinned upstream component/collection API without its unbounded factory."""
    def __init__(self, host: str, port: int):
        if host not in {"127.0.0.1", "localhost", "::1"} or type(port) is not int or not 1 <= port <= 65535:
            raise ConfigurationError("Chroma HTTP requires a loopback host and a valid port")
        # The upstream Client factory accepts only its built-in backend names.
        # Construct the same System and Collection components explicitly so our
        # bounded dispatch also covers initial get/create requests.
        self.system = System(Settings(chroma_api_impl="chromadb.api.fastapi.FastAPI",
            chroma_server_host=host, chroma_server_http_port=port,
            chroma_server_ssl_enabled=False, anonymized_telemetry=False))
        self.api = self.system.instance(BoundedChromaHTTP)
        self.system.start()

    def get_collection(self, name):
        return Collection(client=self.api, model=self.api.get_collection(name), embedding_function=None)

    def create_collection(self, name, metadata):
        return Collection(client=self.api, model=self.api.create_collection(name, metadata=metadata),
                          embedding_function=None)

    def close(self):
        self.system.stop()
