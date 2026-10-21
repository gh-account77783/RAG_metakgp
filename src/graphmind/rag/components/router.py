"""Dispatch engine selection inside the shared reader/admission boundary."""
from ...errors import EngineUnavailableError
from .contracts import QueryContext


class EngineRouter:
    def __init__(self, retrievers: dict):
        self.retrievers = dict(retrievers)

    def retrieve(self, query):
        return self.retrieve_scoped(query, QueryContext())

    def retrieve_scoped(self, query, context: QueryContext):
        retriever = self.retrievers.get(context.engine_id)
        if retriever is None:
            raise EngineUnavailableError("The selected engine is not configured")
        if context.engine_id == "graphmind" and not context.document_ids:
            return retriever.retrieve(query)
        scoped = getattr(retriever, "retrieve_scoped", None)
        if scoped is not None:
            return scoped(query, context)
        if context.document_ids:
            raise EngineUnavailableError("Document selection is not implemented by this engine")
        return retriever.retrieve(query)
