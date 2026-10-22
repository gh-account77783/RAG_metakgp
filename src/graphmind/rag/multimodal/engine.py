"""Original PDF-page retrieval with local image-input evidence generation."""
from __future__ import annotations

import base64
import json

from ...errors import JobLeaseError, ManifestUnavailableError, QueryBudgetError, QueryCancelledError, UnsupportedFileError
from ..components.contracts import Evidence
from ..components.index_contracts import IndexInventory, IndexRecipe, IndexStatus
from ..components.pdf_pages import rasterize_pdf, source_pages


class VisualRetrieval:
    def __init__(self, indexes, sources, artifacts, adapter, inference, staging_dir, *, max_question_chars=2000):
        self.indexes, self.sources, self.artifacts = indexes, sources, artifacts
        self.adapter, self.inference, self.staging_dir = adapter, inference, staging_dir
        self.max_question_chars = max_question_chars

    def recipe(self):
        return IndexRecipe.create("multimodal", "colqwen2-original-pages-v1",
            {"encoder": self.adapter.identity(), "raster": "pdfium-scale1-max1280x1800-png-v1",
             "page_limit": 50, "generation": "local-original-images-v1"})

    def build(self, job, index_id):
        index = self.indexes.index(index_id)
        if index is None or index.recipe != self.recipe():
            raise ManifestUnavailableError("Visual index recipe differs")
        if index.status is IndexStatus.READY:
            self.artifacts.read(index)
            return
        self.indexes.start(job, index_id)
        try:
            snapshot = self.sources.snapshot_for_job(job)
            if snapshot.document.media_type != "application/pdf":
                raise UnsupportedFileError("Visual retrieval accepts text-bearing PDFs only")
            extracted = {page["page"]: page for page in source_pages(
                self.indexes.metadata, snapshot.version, max_pages=50)}
            images = rasterize_pdf(snapshot.content, self.staging_dir, self.inference, max_pages=50)
            pages = [{"page": number, "unit_id": f"page:{snapshot.version.version_id}:{number}",
                      "image": name, "text": extracted.get(number, {}).get("text", "")}
                     for number, name in enumerate(images, 1)]
            self.indexes.metadata.assert_job_lease(job)
            vectors = self.adapter.build(images)
            manifest = self.artifacts.publish(job, index_id, [item["unit_id"] for item in pages],
                {**images, "vectors.safetensors": vectors,
                 "pages.json": json.dumps(pages, ensure_ascii=False).encode("utf-8")})
            self.indexes.complete(job, index_id, manifest.inventory, artifact_ref=manifest.reference,
                                  artifact_digest=manifest.digest)
        except Exception as exc:
            if not isinstance(exc, JobLeaseError):
                self.indexes.fail(job, index_id, getattr(exc, "code", "internal_error"))
            raise

    def retrieve(self, query):
        raise QueryBudgetError("Select one indexed PDF for visual retrieval")

    def retrieve_scoped(self, query, context):
        if len(context.document_ids) != 1:
            raise QueryBudgetError("Select one indexed PDF for visual retrieval")
        if not query.strip():
            return []
        if len(query) > self.max_question_chars:
            raise QueryBudgetError("Question exceeds its character budget")
        document, version = self.sources.active(context.document_ids[0])
        index = self.indexes.ready(document.document_id, "multimodal")
        if index is None or index.recipe != self.recipe():
            raise ManifestUnavailableError("Visual index is not ready for the current source")
        files = self.artifacts.read(index)
        pages = json.loads(files["pages.json"])
        if IndexInventory.from_unit_ids([item["unit_id"] for item in pages]) != index.inventory:
            raise ManifestUnavailableError("Visual page inventory differs")
        if context.cancel_event is not None and context.cancel_event.is_set():
            raise QueryCancelledError("Visual retrieval was cancelled")
        scores = self.adapter.rank(query, files["vectors.safetensors"], page_count=len(pages))
        self.sources.assert_current((version.version_id,))
        if context.cancel_event is not None and context.cancel_event.is_set():
            raise QueryCancelledError("Visual retrieval was cancelled")
        # Initial visual scope answers from the highest-ranked original page.
        # Sending lower-ranked pages made the qualified local model abstain.
        return [Evidence(pages[number - 1]["unit_id"], document.document_id, version.version_id,
            document.display_name, f"page {number}", pages[number - 1]["text"] or "Original PDF page image",
            "visual", score, page_number=number,
            image_base64=base64.b64encode(files[pages[number - 1]["image"]]).decode("ascii"))
                for number, score in scores[:1]]
