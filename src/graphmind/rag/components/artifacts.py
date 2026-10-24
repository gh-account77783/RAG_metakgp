"""Immutable private derived artifacts shared by engine adapters."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import shutil
import uuid
from collections.abc import Mapping, Sequence

from ...errors import ManifestUnavailableError, PublicationError
from ...private_paths import mkdir_private
from .contracts import Job
from .index_contracts import EngineIndex, IndexInventory, IndexStatus
from .indexes import IndexRepository


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


@dataclass(frozen=True, slots=True)
class ArtifactManifest:
    reference: str
    digest: str
    inventory: IndexInventory


class DerivedArtifactStore:
    def __init__(self, data_dir: Path, indexes: IndexRepository, *, max_bytes: int = 512 * 1024 * 1024):
        self.root = data_dir.resolve() / "derived"
        self.indexes = indexes
        self.max_bytes = max_bytes

    @staticmethod
    def _name(name: str) -> str:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
            raise PublicationError("Artifact requires a bounded file name")
        if name == "manifest.json":
            raise PublicationError("Artifact manifest is reserved")
        return name

    def _directory(self, index_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", index_id):
            raise ManifestUnavailableError("Invalid derived-index identity")
        directory = self.root / index_id
        if self.root.is_symlink() or directory.is_symlink() or directory.resolve().parent != self.root.resolve():
            raise ManifestUnavailableError("Derived-index path is unavailable")
        return directory

    def publish(self, job: Job, index_id: str, units: Sequence[str], files: Mapping[str, bytes]) -> ArtifactManifest:
        """Write a complete immutable directory before the metadata ready pointer."""
        with self.indexes.metadata.transaction() as connection:
            index = self.indexes._record(self.indexes._owned(connection, job, index_id))
        if index.status not in {IndexStatus.BUILDING, IndexStatus.READY}:
            raise PublicationError("Artifact index is not building")
        inventory = IndexInventory.from_unit_ids(units)
        if not files or len(files) > 4096:
            raise PublicationError("Artifact inventory is empty or exceeds its file limit")
        records = {}
        total = 0
        for name, content in files.items():
            self._name(name)
            if not isinstance(content, bytes):
                raise PublicationError("Artifacts require immutable bytes")
            total += len(content)
            if total > self.max_bytes:
                raise PublicationError("Derived artifacts exceed their byte limit")
            records[name] = {"size": len(content), "sha256": _digest(content)}
        manifest = _json({"schema_version": 1, "index_id": index_id,
                          "source_version_id": index.source_version_id,
                          "recipe_fingerprint": index.recipe.fingerprint,
                          "units": list(units), "files": records})
        if len(manifest) > 4 * 1024 * 1024:
            raise PublicationError("Derived manifest exceeds its byte limit")
        receipt = ArtifactManifest(f"derived/{index_id}/manifest.json", _digest(manifest), inventory)
        destination = self._directory(index_id)
        mkdir_private(self.root, self.root.parent, exist_ok=True)
        temporary = self.root / f".build-{index_id}-{uuid.uuid4().hex}"
        mkdir_private(temporary, self.root.parent)
        try:
            for name, content in files.items():
                (temporary / name).write_bytes(content)
            (temporary / "manifest.json").write_bytes(manifest)
            # An expired attempt can retain staging bytes but cannot publish them.
            with self.indexes.metadata.transaction() as connection:
                self.indexes._owned(connection, job, index_id)
                if destination.exists():
                    existing = self._read(destination / "manifest.json", 4 * 1024 * 1024)
                    if existing != manifest:
                        raise PublicationError("Derived artifacts are immutable; use a new recipe")
                else:
                    temporary.rename(destination)
            self.read(index, manifest_digest=receipt.digest, inventory=inventory)
            return receipt
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    @staticmethod
    def _read(path: Path, limit: int) -> bytes:
        if path.is_symlink() or not path.is_file():
            raise ManifestUnavailableError("Derived artifact is missing")
        with path.open("rb") as stream:
            content = stream.read(limit + 1)
        if len(content) > limit:
            raise ManifestUnavailableError("Derived artifact exceeds its byte limit")
        return content

    def read(self, index: EngineIndex, *, manifest_digest: str | None = None,
             inventory: IndexInventory | None = None) -> dict[str, bytes]:
        """Authenticate the manifest and its complete file closure before use."""
        try:
            directory = self._directory(index.index_id)
            expected = manifest_digest or index.artifact_digest
            if not expected or (index.artifact_ref is not None
                                and index.artifact_ref != f"derived/{index.index_id}/manifest.json"):
                raise ValueError("Missing manifest identity")
            raw = self._read(directory / "manifest.json", 4 * 1024 * 1024)
            if _digest(raw) != expected:
                raise ValueError("Manifest digest differs")
            manifest = json.loads(raw)
            if (manifest["schema_version"] != 1 or manifest["index_id"] != index.index_id
                    or manifest["source_version_id"] != index.source_version_id
                    or manifest["recipe_fingerprint"] != index.recipe.fingerprint
                    or IndexInventory.from_unit_ids(manifest["units"]) != (inventory or index.inventory)):
                raise ValueError("Manifest lineage differs")
            result, total = {}, 0
            if not 1 <= len(manifest["files"]) <= 4096:
                raise ValueError("Invalid file inventory")
            for name, record in manifest["files"].items():
                self._name(name)
                size = record["size"]
                if type(size) is not int or size < 0 or total + size > self.max_bytes:
                    raise ValueError("Invalid artifact size")
                content = self._read(directory / name, size)
                if len(content) != size or _digest(content) != record["sha256"]:
                    raise ValueError("Artifact digest differs")
                result[name], total = content, total + size
            if {item.name for item in directory.iterdir()} != {*result, "manifest.json"}:
                raise ValueError("Artifact closure differs")
            return result
        except (OSError, KeyError, TypeError, ValueError, PublicationError) as exc:
            raise ManifestUnavailableError("Derived artifacts failed integrity verification") from exc

    def delete_document(self, job: Job) -> None:
        self.indexes.metadata.assert_job_lease(job)
        document = self.indexes.metadata.document(job.document_id)
        if job.operation != "delete_document" or document is None or document.deleted_at is None:
            raise PublicationError("Artifact cleanup requires a document deletion job")
        with self.indexes.metadata.connection() as connection:
            identifiers = [row[0] for row in connection.execute(
                """SELECT index_id FROM engine_indexes WHERE source_version_id IN
                   (SELECT version_id FROM versions WHERE document_id=?)""", (job.document_id,))]
        for index_id in identifiers:
            self.indexes.metadata.assert_job_lease(job)
            directory = self._directory(index_id)
            if directory.exists():
                shutil.rmtree(directory)
