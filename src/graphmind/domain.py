"""Compatibility aliases for graphmind.rag.components.contracts.

Keep a single definition of each domain type, including enum/class identity.
"""
from .rag.components.contracts import (
    AdapterStatus, Chunk, Document, Evidence, Job, JobStatus, Outcome,
    ProviderAnswer, QueryResult, SearchHit, Version, VersionStatus,
)

__all__ = [
    "AdapterStatus", "Chunk", "Document", "Evidence", "Job", "JobStatus",
    "Outcome", "ProviderAnswer", "QueryResult", "SearchHit", "Version",
    "VersionStatus",
]

