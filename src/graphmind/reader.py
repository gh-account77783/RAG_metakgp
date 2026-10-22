"""Shared reader operations used by browser and MCP interfaces."""

from __future__ import annotations

import threading
from contextlib import closing
from typing import Any

from .errors import ReaderQueryError
from .rag.components.contracts import Outcome, QueryContext
from .sources import SourceService


class ReaderService:
    def __init__(self, application: Any, *, max_fetch_chars: int = 24_000) -> None:
        self.application = application
        self.max_fetch_chars = max_fetch_chars
        self.sources = getattr(application, "sources", None) or SourceService(application.metadata)

    @staticmethod
    def _evidence(item: Any) -> dict[str, Any]:
        return item.as_dict()

    def search_documents(self, query: str, *, limit: int = 8,
                         cancel_event: threading.Event | None = None,
                         engine_id: str = "graphmind", document_ids: tuple[str, ...] = ()) -> dict[str, Any]:
        if limit not in range(1, 21):
            raise ValueError("limit must be between 1 and 20")
        context = QueryContext(engine_id, document_ids)
        options = {"context": context} if engine_id != "graphmind" or document_ids else {}
        evidence = self.application.answers.search(query, cancel_event=cancel_event, **options)[:limit]
        return {"query": query, "results": [self._evidence(item) for item in evidence]}

    def ask(self, question: str, *, cancel_event: threading.Event | None = None,
            engine_id: str = "graphmind", document_ids: tuple[str, ...] = ()) -> dict[str, Any]:
        context = QueryContext(engine_id, document_ids)
        options = {"context": context} if engine_id != "graphmind" or document_ids else {}
        result = self.application.answers.query(question, cancel_event=cancel_event, **options)
        if result.outcome is Outcome.ERROR:
            raise ReaderQueryError(str(result.verification.get("error_code", "query_failed")))
        return result.as_dict()

    def validate_result(self, result: dict[str, Any]) -> None:
        """Run again after asynchronous authorization checks, before delivery."""
        versions = {item["version_id"] for item in result.get("results", [])}
        versions.update(item["version_id"] for item in result.get("citations", []))
        if "version_id" in result:
            versions.add(result["version_id"])
        versions.update(item["version_id"] for item in result.get("documents", []))
        self.sources.assert_current(versions)

    def catalog(self) -> dict[str, Any]:
        from .rag.registry import ENGINE_CATALOG
        application = self.application
        configured = set(application.settings.enabled_engines)
        backends = application.router.retrievers
        recipes = {}
        availability = {}
        runtime = application.engine_readiness()
        for spec in ENGINE_CATALOG:
            backend = backends.get(spec.engine_id)
            if (spec.engine_id not in configured or backend is None
                    or not runtime.get(spec.engine_id) or not all(item.available for item in runtime[spec.engine_id])):
                availability[spec.engine_id] = False
                continue
            try:
                recipes[spec.engine_id] = backend.recipe() if hasattr(backend, "recipe") else None
                availability[spec.engine_id] = True
            except Exception:
                availability[spec.engine_id] = False
        heads = {spec.engine_id: {index.source_version_id: index for index in application.indexes.current(spec.engine_id)}
                 for spec in ENGINE_CATALOG}
        documents = []
        for document in application.metadata.list_documents():
            if document.deleted_at is not None or document.active_version_id is None:
                continue
            states = {}
            pending = application.indexes.for_document(document.document_id)
            for spec in ENGINE_CATALOG:
                supported = spec.engine_id == "graphmind" or document.media_type == "application/pdf"
                index = heads[spec.engine_id].get(document.active_version_id)
                recipe = recipes.get(spec.engine_id)
                ready = bool(supported and availability[spec.engine_id] and index is not None
                             and (recipe is None or index.recipe == recipe))
                state = ("unconfigured" if spec.engine_id not in configured else "unsupported" if not supported
                         else "unavailable" if not availability[spec.engine_id] else "ready" if ready
                         else "needs_reindex" if index is not None else "not_indexed")
                if state == "not_indexed":
                    matching = [item for item in pending if item.source_version_id == document.active_version_id
                                and item.recipe.engine_id == spec.engine_id
                                and (recipe is None or item.recipe == recipe)]
                    if matching:
                        state = {"staged": "queued", "building": "building", "failed": "failed"}.get(
                            matching[-1].status.value, state)
                states[spec.engine_id] = {"state": state, "ready": ready}
            documents.append({"document_id": document.document_id, "version_id": document.active_version_id,
                              "display_name": document.display_name, "media_type": document.media_type,
                              "engines": states})
        result = {"engines": [{"engine_id": spec.engine_id, "display_name": spec.display_name,
                    "implemented": spec.implemented, "configured": spec.engine_id in configured,
                    "available": availability[spec.engine_id],
                    "max_documents": 16 if spec.engine_id == "graphmind" else 1,
                    "requires_document": spec.engine_id != "graphmind"} for spec in ENGINE_CATALOG],
                  "documents": documents}
        self.validate_result(result)
        return result

    def fetch_asset(self, document_id: str, *, version_id: str | None = None,
                    page_number: int | None = None) -> dict[str, Any]:
        document, version = self.sources.active(document_id, version_id)
        if page_number is None:
            content = self.sources.snapshot(document_id, version.version_id).content
            media_type, attachment = "application/octet-stream", True
        else:
            if (type(page_number) is not int or not 1 <= page_number <= self.application.settings.max_pdf_pages
                    or document.media_type != "application/pdf"):
                raise ValueError("Select an original PDF page within the supported limit")
            index = self.application.indexes.ready(document_id, "multimodal")
            name = f"page-{page_number}.png"
            if index is not None:
                files = self.application.artifacts.read(index)
                if name not in files:
                    raise ValueError("Page is outside the source bounds")
                content = files[name]
            else:
                from .rag.components.pdf_pages import rasterize_pdf
                snapshot = self.sources.snapshot(document_id, version.version_id)
                content = rasterize_pdf(snapshot.content, self.application.settings.staging_dir,
                    self.application.inference, max_pages=self.application.settings.max_pdf_pages,
                    page_numbers=(page_number,))[name]
            media_type, attachment = "image/png", False
        self.sources.assert_current((version.version_id,))
        return {"document_id": document_id, "version_id": version.version_id,
                "display_name": document.display_name, "media_type": media_type,
                "attachment": attachment, "content": content}

    def fetch_document(
        self,
        document_id: str,
        *,
        version_id: str | None = None,
        max_chars: int | None = None,
    ) -> dict[str, Any]:
        document, version = self.sources.active(document_id, version_id)
        selected_version = version.version_id
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
        self.sources.assert_current((selected_version,))
        return {
            "document_id": document.document_id,
            "version_id": selected_version,
            "display_name": document.display_name,
            "media_type": document.media_type,
            "text": "\n\n".join(sections),
            "truncated": truncated,
        }
