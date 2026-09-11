"""Private original-file storage adapter."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Protocol

from .domain import AdapterStatus
from .errors import DependencyUnavailableError


class FileStore(Protocol):
    def private_ref(self, document_id: str, version_id: str) -> str: ...
    def put_bytes(self, document_id: str, version_id: str, content: bytes) -> str: ...
    def readiness(self) -> AdapterStatus: ...


class PrivateFileStore:
    def __init__(self, data_dir: Path, files_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.files_dir = files_dir.resolve()

    def private_ref(self, document_id: str, version_id: str) -> str:
        path = self.files_dir / document_id / version_id / "original.txt"
        return str(path.relative_to(self.data_dir))

    def put_bytes(self, document_id: str, version_id: str, content: bytes) -> str:
        directory = self.files_dir / document_id / version_id
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination = directory / "original.txt"
            temporary = directory / f".original.{uuid.uuid4().hex}.tmp"
            temporary.write_bytes(content)
            os.replace(temporary, destination)
            return str(destination.relative_to(self.data_dir))
        except OSError as exc:
            raise DependencyUnavailableError("Private file storage is unavailable") from exc

    def readiness(self) -> AdapterStatus:
        try:
            self.files_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            probe = self.files_dir / f".readiness-{uuid.uuid4().hex}"
            probe.write_bytes(b"")
            probe.unlink()
            return AdapterStatus("private-files", True, "ready")
        except OSError:
            return AdapterStatus("private-files", False, "unavailable")
