"""Actual Neo4j commit/rollback and concurrent-reader acceptance, explicitly opt-in."""
from __future__ import annotations

import os
import threading
import time
import unittest
import uuid
from pathlib import Path

from graphmind.config import Settings
from graphmind.domain import Chunk, Document, Version, VersionStatus
from graphmind.errors import DependencyUnavailableError
from graphmind.storage import Neo4jGraphStore


@unittest.skipUnless(os.environ.get("GRAPHMIND_RUN_INTEGRATION") == "1", "real Neo4j explicitly required")
class RealGraphAtomicityTests(unittest.TestCase):
    def test_reference_updates_rollback_and_are_not_partially_visible(self):
        dotenv = os.environ.get("GRAPHMIND_TEST_DOTENV")
        if not dotenv:
            self.fail("Explicit private test dotenv is required")
        settings = Settings.load(dotenv_path=Path(dotenv), environ={})
        store = Neo4jGraphStore(settings.neo4j_uri, settings.neo4j_username, settings.neo4j_password,
                               settings.neo4j_database)
        installation = "graphmind-atomicity-" + uuid.uuid4().hex
        saved_driver = None
        try:
            saved_driver = store._get_driver()
            fixtures = {}
            for name, references in (("A", ("B",)), ("B", ()), ("X", ("Y",)), ("Y", ())):
                document = Document(name, "test", name, name, "text/plain", name, "now")
                version = Version(name + "-version", name, "h", "e", "c", "f", "unused", 1, 1,
                                  "digest", VersionStatus.READY, "now")
                chunks = [Chunk(name + "-chunk", version.version_id, name, 0, "fixture", "line 1", 0, 7, references)]
                fixtures[name] = (document, version, chunks)
                store.upsert_version(installation, *fixtures[name])
            driver = store._get_driver()

            def edges():
                records, _, _ = driver.execute_query(
                    "MATCH (s:GraphMindChunk {installation_id: $installation})-[:REFERENCES]->"
                    "(t:GraphMindDocument {installation_id: $installation}) RETURN count(*) AS count",
                    installation=installation, database_=settings.neo4j_database)
                return records[0]["count"]

            self.assertEqual(edges(), 2)

            class TransactionProxy:
                def __init__(self, transaction, *, fail):
                    self.transaction, self.fail, self.calls = transaction, fail, 0

                def run(self, query, **params):
                    self.calls += 1
                    if self.calls == 3:
                        # Node upsert and scoped deletion have executed in the
                        # actual transaction; now fail or pause before resolution.
                        if self.fail:
                            raise RuntimeError("synthetic resolution failure")
                        time.sleep(0.3)
                    return self.transaction.run(query, **params)

            class SessionProxy:
                def __init__(self, session, fail):
                    self.session, self.fail = session, fail

                def __enter__(self):
                    self.session.__enter__()
                    return self

                def __exit__(self, *args):
                    return self.session.__exit__(*args)

                def execute_write(self, callback):
                    return self.session.execute_write(lambda tx: callback(TransactionProxy(tx, fail=self.fail)))

            class DriverProxy:
                def __init__(self, *, fail):
                    self.fail = fail

                def session(self, **kwargs):
                    return SessionProxy(driver.session(**kwargs), self.fail)

            store._driver = DriverProxy(fail=True)
            with self.assertRaises(DependencyUnavailableError):
                store.upsert_version(installation, *fixtures["A"])
            self.assertEqual(edges(), 2, "Failed resolution must roll back the reference deletion")
            stop = threading.Event()
            observed, errors = [], []

            def read():
                while not stop.is_set():
                    try:
                        observed.append(edges())
                    except Exception as exc:
                        errors.append(type(exc).__name__)
                        return

            reader = threading.Thread(target=read, daemon=True)
            store._driver = DriverProxy(fail=False)
            reader.start()
            try:
                store.upsert_version(installation, *fixtures["A"])
            finally:
                stop.set()
                reader.join(timeout=10)
            self.assertFalse(reader.is_alive(), "Concurrent reader did not stop")
            self.assertEqual(errors, [])
            self.assertTrue(observed, "Concurrent reader never executed")
            self.assertEqual(set(observed), {2}, "Partial graph or unrelated edge loss became visible")
            self.assertEqual(edges(), 2)
        finally:
            if saved_driver is not None:
                store._driver = saved_driver
                store.delete_installation(installation)  # Exact newly generated installation only.
            store.close()


if __name__ == "__main__":
    unittest.main()
