"""GraphMind's general-purpose RAG application foundation."""

from .config import Settings
from .domain import Outcome, QueryResult

__all__ = ["Outcome", "QueryResult", "Settings"]
__version__ = "0.1.0"

