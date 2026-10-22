"""Shared local-volume reserve for host admission and readiness checks."""
from __future__ import annotations

import shutil

from .errors import StorageCapacityError
from .rag.components.contracts import AdapterStatus


def disk_capacity(settings) -> dict[str, int]:
    # Application-managed originals, staging, SQLite and embedded artifacts
    # share data_dir. External Chroma/Neo4j/model volumes need their own checks.
    available = shutil.disk_usage(settings.data_dir).free
    return {"free_bytes": available, "reserve_bytes": settings.min_free_disk_bytes}


def require_storage(settings, *, additional_bytes: int = 0) -> None:
    try:
        capacity = disk_capacity(settings)
    except OSError as exc:
        raise StorageCapacityError("Application storage capacity cannot be checked") from exc
    if capacity["free_bytes"] < capacity["reserve_bytes"] + additional_bytes:
        raise StorageCapacityError("Application storage is below its free-space reserve; free space and retry")


def storage_readiness(settings) -> AdapterStatus:
    try:
        require_storage(settings)
    except StorageCapacityError:
        return AdapterStatus("storage-capacity", False, "unavailable or below reserve")
    return AdapterStatus("storage-capacity", True, "ready")
