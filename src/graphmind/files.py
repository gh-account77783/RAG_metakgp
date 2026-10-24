"""Private original-file storage adapter."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Protocol

from .rag.components.contracts import AdapterStatus
from .errors import DependencyUnavailableError
from .private_paths import mkdir_private


class FileStore(Protocol):
    def private_ref(self, document_id: str, version_id: str, suffix: str = ".bin") -> str: ...
    def put_bytes(
        self, document_id: str, version_id: str, content: bytes, suffix: str = ".bin"
    ) -> str: ...
    def read_bytes(self, private_ref: str, *, max_bytes: int) -> bytes: ...
    def delete_document(self, document_id: str) -> None: ...
    def readiness(self) -> AdapterStatus: ...


class PrivateFileStore:
    def __init__(self, data_dir: Path, files_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.files_dir = files_dir.resolve()

    @staticmethod
    def _source_name(suffix: str) -> str:
        normalized = suffix.casefold()
        if not normalized.startswith(".") or not normalized[1:].isalnum():
            normalized = ".bin"
        return f"original{normalized}"

    def private_ref(self, document_id: str, version_id: str, suffix: str = ".bin") -> str:
        path = self.files_dir / document_id / version_id / self._source_name(suffix)
        return str(path.relative_to(self.data_dir))

    def put_bytes(
        self, document_id: str, version_id: str, content: bytes, suffix: str = ".bin"
    ) -> str:
        directory = self.files_dir / document_id / version_id
        try:
            mkdir_private(directory, self.data_dir, exist_ok=True)
            destination = directory / self._source_name(suffix)
            temporary = directory / f".original.{uuid.uuid4().hex}.tmp"
            temporary.write_bytes(content)
            os.replace(temporary, destination)
            return str(destination.relative_to(self.data_dir))
        except OSError as exc:
            raise DependencyUnavailableError("Private file storage is unavailable") from exc

    def read_bytes(self, private_ref: str, *, max_bytes: int) -> bytes:
        if max_bytes < 1:
            raise ValueError("Original-file byte limit must be positive")
        reference = Path(private_ref)
        try:
            if reference.is_absolute() or ".." in reference.parts:
                raise ValueError("Original-file reference must be private and relative")
            candidate = self.data_dir / reference
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self.files_dir)
            if candidate.is_symlink() or not resolved.is_file():
                raise ValueError("Original file is unavailable")
            with resolved.open("rb") as stream:
                content = stream.read(max_bytes + 1)
            if len(content) > max_bytes:
                raise ValueError("Original file exceeds its byte limit")
            return content
        except (OSError, ValueError, RuntimeError) as exc:
            raise DependencyUnavailableError("Private original is unavailable or exceeds its limit") from exc

    def delete_document(self, document_id: str) -> None:
        target = (self.files_dir / document_id).resolve()
        try:
            target.relative_to(self.files_dir)
            if target.is_dir():
                shutil.rmtree(target)
        except (OSError, ValueError) as exc:
            raise DependencyUnavailableError("Private document cleanup failed") from exc

    def readiness(self) -> AdapterStatus:
        try:
            mkdir_private(self.files_dir, self.data_dir, exist_ok=True)
            probe = self.files_dir / f".readiness-{uuid.uuid4().hex}"
            probe.write_bytes(b"")
            probe.unlink()
            return AdapterStatus("private-files", True, "ready")
        except OSError:
            return AdapterStatus("private-files", False, "unavailable")
