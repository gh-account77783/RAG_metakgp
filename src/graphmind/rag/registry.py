"""Explicit engine catalogue metadata, not installation or runtime readiness.

Only GraphMind is implemented. Reserving namespaces must not make the future
engines selectable or trigger upstream imports, downloads or network access.
"""
from dataclasses import dataclass

from ..errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class EngineSpec:
    engine_id: str
    display_name: str
    implemented: bool


ENGINE_CATALOG = (
    EngineSpec("graphmind", "Connected Knowledge", True),
    EngineSpec("tree_navigation", "Long Documents", False),
    EngineSpec("multimodal", "Visual Documents", False),
)


def engine_spec(engine_id: str) -> EngineSpec:
    for spec in ENGINE_CATALOG:
        if spec.engine_id == engine_id:
            return spec
    raise ConfigurationError("Unknown RAG engine")
