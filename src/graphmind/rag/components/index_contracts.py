"""Immutable recipes and inventories shared by derived-index backends."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import re
from typing import Any


class IndexStatus(StrEnum):
    STAGED = "staged"
    BUILDING = "building"
    READY = "ready"
    FAILED = "failed"
    INVALIDATED = "invalidated"


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


@dataclass(frozen=True, slots=True)
class IndexRecipe:
    engine_id: str
    revision: str
    configuration_json: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.engine_id):
            raise ValueError("Invalid engine identifier")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.revision):
            raise ValueError("Invalid index recipe revision")
        if len(self.configuration_json.encode("utf-8")) > 64 * 1024:
            raise ValueError("Index recipe configuration is too large")
        value = json.loads(self.configuration_json)
        if not isinstance(value, dict):
            raise ValueError("Index recipe configuration must be an object")
        object.__setattr__(self, "configuration_json", _json(value))

    @classmethod
    def create(cls, engine_id: str, revision: str, configuration: Mapping[str, Any]) -> IndexRecipe:
        # Pass index-affecting values only, never credentials or whole app settings.
        return cls(engine_id, revision, _json(dict(configuration)))

    @property
    def serialized(self) -> str:
        return _json({"schema_version": 1, "engine_id": self.engine_id, "revision": self.revision,
                      "configuration": json.loads(self.configuration_json)})

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.serialized.encode("utf-8")).hexdigest()

    def index_id(self, installation_id: str, source_version_id: str) -> str:
        if not installation_id or not source_version_id:
            raise ValueError("Index identity requires installation and source version")
        value = _json({"installation_id": installation_id, "source_version_id": source_version_id,
                       "recipe": self.fingerprint})
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @classmethod
    def deserialize(cls, value: str) -> IndexRecipe:
        decoded = json.loads(value)
        if decoded["schema_version"] != 1:
            raise ValueError("Unknown index recipe schema")
        return cls.create(decoded["engine_id"], decoded["revision"], decoded["configuration"])


@dataclass(frozen=True, slots=True)
class IndexInventory:
    unit_count: int
    unit_digest: str

    def __post_init__(self) -> None:
        if isinstance(self.unit_count, bool) or not isinstance(self.unit_count, int) or self.unit_count < 0:
            raise ValueError("Index unit count must be a nonnegative integer")
        if not re.fullmatch(r"[0-9a-f]{64}", self.unit_digest):
            raise ValueError("Index unit digest must be SHA-256")

    @classmethod
    def from_unit_ids(cls, identifiers: Sequence[str]) -> IndexInventory:
        if isinstance(identifiers, (str, bytes)) or not isinstance(identifiers, Sequence):
            raise ValueError("Index units require an ordered sequence")
        if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}", value)
               for value in identifiers):
            raise ValueError("Index unit identifiers must be bounded opaque values")
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("Duplicate index units")
        digest = hashlib.sha256("\n".join(identifiers).encode("utf-8")).hexdigest()
        return cls(len(identifiers), digest)


@dataclass(frozen=True, slots=True)
class EngineIndex:
    index_id: str
    source_version_id: str
    recipe: IndexRecipe
    status: IndexStatus
    inventory: IndexInventory | None
    created_at: str
    updated_at: str
    failure_code: str | None = None
