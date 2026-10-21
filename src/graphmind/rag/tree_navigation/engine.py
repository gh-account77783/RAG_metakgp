"""Bounded PageIndex navigation with original PDF-page evidence and no vectors."""
from __future__ import annotations

import hashlib
import json
import re

from ...errors import (EngineUnavailableError, GraphMindError, JobLeaseError, ManifestUnavailableError,
                       ParserLimitError, ProviderResponseError, QueryBudgetError, QueryCancelledError, UnsupportedFileError)
from ..components.contracts import Evidence, QueryContext
from ..components.index_contracts import IndexInventory, IndexRecipe, IndexStatus


REVISION = "pageindex-flash-pages-v1"


class TreeNavigation:
    def __init__(self, indexes, sources, artifacts, adapter, provider, *, max_pdf_pages=1000,
                 max_question_chars=2000, max_context_chars=24000, max_navigation_steps=6):
        self.indexes, self.sources, self.artifacts = indexes, sources, artifacts
        self.adapter, self.provider = adapter, provider
        self.max_pdf_pages, self.max_question_chars = max_pdf_pages, max_question_chars
        self.max_context_chars, self.max_navigation_steps = max_context_chars, max_navigation_steps

    def recipe(self):
        return IndexRecipe.create("tree_navigation", REVISION,
            {"pageindex": "0.4.4", "extraction": "flash-summary-off-optimize-off",
             "evidence": "original-pdf-pages-v1", "fanout": 8})

    def _pages(self, version):
        chunks = self.indexes.metadata.all_chunks_for_version(version.version_id)
        inventory = IndexInventory.from_unit_ids([item.chunk_id for item in chunks])
        if (inventory.unit_count, inventory.unit_digest) != (version.expected_chunk_count, version.chunk_digest):
            raise ManifestUnavailableError("PDF source inventory differs")
        pages = {}
        for chunk in chunks:
            match = re.fullmatch(r"page (\d+)(?:, part \d+)?", chunk.locator)
            if match is None:
                raise UnsupportedFileError("Tree navigation requires original PDF-page text")
            page = int(match.group(1))
            if page not in range(1, self.max_pdf_pages + 1):
                raise ParserLimitError("PDF page exceeds the configured limit")
            existing = pages.setdefault(page, {"text": "", "start": chunk.start_offset})
            offset = chunk.start_offset - existing["start"]
            if offset > len(existing["text"]):
                existing["text"] += " " * (offset - len(existing["text"]))
            overlap = max(0, len(existing["text"]) - offset)
            existing["text"] += chunk.text[overlap:]
        return [{"page": number, "unit_id": f"page:{version.version_id}:{number}",
                 "locator": f"page {number}", "text": record["text"]}
                for number, record in sorted(pages.items())]

    def _tree(self, raw, pages):
        identifiers, count = set(), 0

        def grouped(nodes):
            while len(nodes) > 8:
                groups = []
                for offset in range(0, len(nodes), 8):
                    children = nodes[offset:offset + 8]
                    identifier = hashlib.sha256("\n".join(item["node_id"] for item in children).encode()).hexdigest()[:24]
                    groups.append({"node_id": "g_" + identifier,
                        "title": f'Pages {children[0]["start_index"]}-{children[-1]["end_index"]}',
                        "start_index": min(item["start_index"] for item in children),
                        "end_index": max(item["end_index"] for item in children), "nodes": children})
                nodes = groups
            return nodes

        def normalize(nodes, depth=0):
            nonlocal count
            if depth > 32 or not isinstance(nodes, list):
                raise ParserLimitError("PDF outline exceeds its structure limit")
            result = []
            for item in nodes:
                count += 1
                if count > 4096:
                    raise ParserLimitError("PDF outline exceeds its node limit")
                start, end, identifier = item["start_index"], item["end_index"], item["node_id"]
                if (type(start) is not int or type(end) is not int or not 1 <= start <= end <= self.max_pdf_pages
                        or not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier)
                        or identifier in identifiers or not isinstance(item["title"], str)):
                    raise ManifestUnavailableError("PDF outline has invalid source ranges or node identities")
                identifiers.add(identifier)
                children = normalize(item.get("nodes", []), depth + 1)
                matching = [page["page"] for page in pages if start <= page["page"] <= end]
                node = {"node_id": "t_" + identifier, "title": item["title"][:500],
                        "start_index": start, "end_index": end}
                if not children and len(matching) > 1:
                    children = grouped([{"node_id": f"p_{page}", "title": f"Page {page}",
                                         "start_index": page, "end_index": page, "page": page}
                                        for page in matching])
                if children:
                    node["nodes"] = children
                elif matching:
                    node["page"] = matching[0]
                result.append(node)
            return grouped(result)

        return normalize(raw)

    def build(self, job, index_id):
        index = self.indexes.index(index_id)
        if index is None or index.recipe != self.recipe():
            raise ManifestUnavailableError("Tree index recipe differs")
        if index.status is IndexStatus.READY:
            self.artifacts.read(index)
            return
        self.indexes.start(job, index_id)
        try:
            snapshot = self.sources.snapshot_for_job(job)
            if snapshot.document.media_type != "application/pdf":
                raise UnsupportedFileError("Tree navigation accepts text-bearing PDFs only")
            pages = self._pages(snapshot.version)
            structure = self._tree(self.adapter.extract(snapshot.content), pages)
            if not structure or not pages:
                raise UnsupportedFileError("PDF has no readable tree/page representation")
            manifest = self.artifacts.publish(job, index_id, [page["unit_id"] for page in pages],
                {"tree.json": json.dumps({"structure": structure, "pages": pages}, ensure_ascii=False).encode("utf-8")})
            self.indexes.complete(job, index_id, manifest.inventory, artifact_ref=manifest.reference,
                                  artifact_digest=manifest.digest)
        except Exception as exc:
            if not isinstance(exc, JobLeaseError):
                self.indexes.fail(job, index_id, getattr(exc, "code", "internal_error"))
            raise

    def retrieve(self, query):
        raise QueryBudgetError("Select one indexed PDF for tree navigation")

    def retrieve_scoped(self, query, context: QueryContext):
        if len(context.document_ids) != 1:
            raise QueryBudgetError("Select one indexed PDF for tree navigation")
        if not query.strip():
            return []
        if len(query) > self.max_question_chars:
            raise QueryBudgetError("Question exceeds the configured character budget")
        document, version = self.sources.active(context.document_ids[0])
        index = self.indexes.ready(document.document_id, "tree_navigation")
        if index is None or index.recipe != self.recipe():
            raise ManifestUnavailableError("Tree index is not ready for the current source")
        payload = json.loads(self.artifacts.read(index)["tree.json"])
        pages = {item["page"]: item for item in payload["pages"]}
        if IndexInventory.from_unit_ids([item["unit_id"] for item in payload["pages"]]) != index.inventory:
            raise ManifestUnavailableError("Tree page inventory differs")
        if not hasattr(self.provider, "select_nodes"):
            raise EngineUnavailableError("Configured provider does not support tree navigation")
        frontier, selected = payload["structure"], set()
        for _ in range(self.max_navigation_steps):
            if context.cancel_event is not None and context.cancel_event.is_set():
                raise QueryCancelledError("Tree navigation was cancelled")
            if not frontier:
                break
            if len(frontier) > 16:
                raise QueryBudgetError("Tree frontier exceeds its navigation limit")
            candidates = []
            for node in frontier:
                preview = next((page["text"][:320] for number, page in sorted(pages.items())
                                if node["start_index"] <= number <= node["end_index"]), "")
                candidates.append({key: node[key] for key in ("node_id", "title", "start_index", "end_index")}
                                  | {"preview": preview})
            self.sources.assert_current((version.version_id,))
            choice = self.provider.select_nodes(query, candidates, limit=2)
            self.sources.assert_current((version.version_id,))
            if (not isinstance(choice, list) or len(choice) > 2
                    or any(value not in {node["node_id"] for node in frontier} for value in choice)):
                raise ProviderResponseError("Tree navigator selected an unknown node")
            following = []
            for node in frontier:
                if node["node_id"] not in choice:
                    continue
                if node.get("nodes"):
                    following.extend(node["nodes"])
                elif node.get("page") in pages:
                    selected.add(node["page"])
            frontier = following
            if len(selected) >= 3:
                if frontier:
                    raise QueryBudgetError("Tree navigation exceeds its evidence-page limit")
                break
        if frontier:
            raise QueryBudgetError("Tree navigation exceeds its step limit")
        if len(selected) > 3 or sum(len(pages[number]["text"]) for number in selected) > self.max_context_chars:
            raise QueryBudgetError("Selected PDF pages exceed the configured evidence budget")
        evidence = [Evidence(pages[number]["unit_id"], document.document_id, version.version_id,
                            document.display_name, f"page {number}", pages[number]["text"], "tree", 1.0)
                    for number in sorted(selected)]
        self.sources.assert_current((version.version_id,))
        return evidence
