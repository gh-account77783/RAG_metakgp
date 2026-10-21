"""FTS5 BM25 cache scoped by verified engine heads in the existing database."""
from __future__ import annotations

from collections.abc import Sequence
import re
import sqlite3

from ...errors import ManifestUnavailableError, PublicationError
from ..components.contracts import Job, SearchHit
from ..components.index_contracts import IndexStatus
from ..components.indexes import IndexRepository


class LexicalIndex:
    def __init__(self, indexes: IndexRepository):
        self.indexes = indexes

    def replace(self, job: Job, index_id: str, title: str, passages: Sequence[dict]) -> None:
        with self.indexes.metadata.transaction() as connection:
            index = self.indexes._record(self.indexes._owned(connection, job, index_id))
            if index.status is not IndexStatus.BUILDING:
                raise PublicationError("Lexical writes require a building index")
            connection.execute("DELETE FROM graphmind_lexical WHERE index_id=?", (index_id,))
            connection.executemany(
                "INSERT INTO graphmind_lexical(unit_id,index_id,title,locator,body) VALUES(?,?,?,?,?)",
                [(item["unit_id"], index_id, title, item["locator"], item["text"]) for item in passages])

    def search(self, query: str, *, index_ids: set[str], limit: int = 50) -> list[SearchHit]:
        if not index_ids or limit < 1:
            return []
        # Treat user text as terms, never executable FTS operators or column syntax.
        terms = list(dict.fromkeys(re.findall(r"[^\W_]+", query.casefold())))[:64]
        if not terms:
            return []
        expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
        placeholders = ",".join("?" for _ in index_ids)
        try:
            with self.indexes.metadata.connection() as connection:
                rows = connection.execute(
                    f"""SELECT f.unit_id, bm25(graphmind_lexical,0,0,3,1,1) AS score
                        FROM graphmind_lexical f JOIN engine_indexes i ON i.index_id=f.index_id
                        JOIN engine_index_heads h ON h.index_id=i.index_id AND h.engine_id=i.engine_id
                        JOIN versions v ON v.version_id=i.source_version_id
                        JOIN documents d ON d.document_id=h.document_id
                        WHERE graphmind_lexical MATCH ? AND f.index_id IN ({placeholders})
                          AND d.active_version_id=v.version_id AND v.document_id=d.document_id
                          AND d.deleted_at IS NULL AND v.status='ready' AND i.status='ready'
                        ORDER BY score, f.unit_id LIMIT ?""",
                    (expression, *sorted(index_ids), min(limit, 100)))
                return [SearchHit(row["unit_id"], -float(row["score"])) for row in rows]
        except sqlite3.Error as exc:
            raise ManifestUnavailableError("Lexical retrieval is unavailable") from exc

    def owners(self, unit_ids: Sequence[str], *, index_ids: set[str]) -> dict[str, str]:
        if not unit_ids or not index_ids:
            return {}
        with self.indexes.metadata.connection() as connection:
            rows = connection.execute(
                f"""SELECT unit_id,index_id FROM graphmind_lexical
                    WHERE unit_id IN ({','.join('?' for _ in unit_ids)})
                      AND index_id IN ({','.join('?' for _ in index_ids)})""",
                (*unit_ids, *sorted(index_ids)))
            return {row["unit_id"]: row["index_id"] for row in rows}

    def delete_document(self, job: Job) -> None:
        with self.indexes.metadata.transaction() as connection:
            self.indexes.metadata._assert_job_lease(connection, job)
            document = connection.execute("SELECT deleted_at FROM documents WHERE document_id=?",
                                           (job.document_id,)).fetchone()
            if job.operation != "delete_document" or document is None or document[0] is None:
                raise PublicationError("Lexical cleanup requires a document deletion job")
            connection.execute(
                """DELETE FROM graphmind_lexical WHERE index_id IN
                   (SELECT i.index_id FROM engine_indexes i JOIN versions v ON v.version_id=i.source_version_id
                    WHERE v.document_id=?)""", (job.document_id,))
