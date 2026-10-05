"""Shared reader operations used by browser and MCP interfaces."""

from __future__ import annotations

import threading
from contextlib import closing
from typing import Any

from .errors import DocumentNotFoundError, ReaderQueryError, RetryableQueryError
from .domain import Outcome


class ReaderService:
    def __init__(self, application: Any, *, max_fetch_chars: int = 24_000) -> None:
        self.application = application
        self.max_fetch_chars = max_fetch_chars

    @staticmethod
    def _evidence(item: Any) -> dict[str, Any]:
        return {
            "citation_id": item.citation_id,
            "document_id": item.document_id,
            "version_id": item.version_id,
            "display_name": item.display_name,
            "locator": item.locator,
            "excerpt": item.excerpt,
            "via": item.via,
            "score": item.score,
        }

    def search_documents(self, query: str, *, limit: int = 8,
                         cancel_event: threading.Event | None = None) -> dict[str, Any]:
        if limit not in range(1, 21):
            raise ValueError("limit must be between 1 and 20")
        evidence = self.application.answers.search(query, cancel_event=cancel_event)[:limit]
        return {"query": query, "results": [self._evidence(item) for item in evidence]}

    def ask(self, question: str, *, cancel_event: threading.Event | None = None) -> dict[str, Any]:
        result = self.application.answers.query(question, cancel_event=cancel_event)
        if result.outcome is Outcome.ERROR:
            raise ReaderQueryError(str(result.verification.get("error_code", "query_failed")))
        return result.as_dict()

    def validate_result(self, result: dict[str, Any]) -> None:
        """Run again after asynchronous authorization checks, before delivery."""
        versions = {item["version_id"] for item in result.get("results", [])}
        versions.update(item["version_id"] for item in result.get("citations", []))
        if "version_id" in result:
            versions.add(result["version_id"])
        if versions and not versions <= self.application.metadata.active_version_ids():
            raise RetryableQueryError("A source version changed before delivery; retry")

    def fetch_document(
        self,
        document_id: str,
        *,
        version_id: str | None = None,
        max_chars: int | None = None,
    ) -> dict[str, Any]:
        document = self.application.metadata.document(document_id)
        if document is None or document.deleted_at is not None or document.active_version_id is None:
            raise DocumentNotFoundError("Document is unavailable")
        selected_version = version_id or document.active_version_id
        if selected_version != document.active_version_id:
            raise DocumentNotFoundError("Document version is unavailable")
        version = self.application.metadata.version(selected_version)
        if version is None or version.status.value != "ready":
            raise DocumentNotFoundError("Document version is unavailable")
        requested_limit = self.max_fetch_chars if max_chars is None else max_chars
        if requested_limit < 1 or requested_limit > self.max_fetch_chars:
            raise ValueError(f"max_chars must be between 1 and {self.max_fetch_chars}")
        sections: list[str] = []
        used = 0
        truncated = False
        with closing(self.application.metadata.iter_chunks_for_version(selected_version)) as chunks:
            for chunk in chunks:
                section = f"[{chunk.locator}]\n{chunk.text.strip()}"
                separator = 2 if sections else 0
                remaining = requested_limit - used - separator
                if remaining <= 0:
                    truncated = True
                    break
                if len(section) > remaining:
                    sections.append(section[:remaining])
                    used += remaining + separator
                    truncated = True
                    break
                sections.append(section)
                used += len(section) + separator
        current = self.application.metadata.document(document_id)
        if (current is None or current.deleted_at is not None
                or current.active_version_id != selected_version
                or selected_version not in self.application.metadata.active_version_ids()):
            raise RetryableQueryError("Document version changed during fetch; retry")
        return {
            "document_id": document.document_id,
            "version_id": selected_version,
            "display_name": document.display_name,
            "media_type": document.media_type,
            "text": "\n\n".join(sections),
            "truncated": truncated,
        }
