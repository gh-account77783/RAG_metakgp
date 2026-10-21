"""GraphMind's general-purpose RAG application foundation."""

from .config import Settings
from .rag.components.contracts import Outcome, QueryResult

__all__ = ["Outcome", "QueryResult", "Settings"]
__version__ = "0.1.0"

