"""Regression coverage for the October foundation review (synthetic data only)."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from graphmind.domain import Chunk, Document, Evidence, JobStatus, VersionStatus
from graphmind.embeddings import HashEmbedding, OllamaEmbedding
from graphmind.errors import (
    AccountStateError, DependencyUnavailableError, DocumentTooLargeError,
    EmbeddingResponseError, EmbeddingUnavailableError, JobLeaseError,
    QueryCancelledError, QueryCapacityError, RetryableQueryError,
)
from graphmind.http_client import HttpUnavailableError
from graphmind.identity import KeycloakAdminClient, KeycloakAuthority
from graphmind.jobs import DurableJobExecutor
from graphmind.locking import exclusive_file_lock
from graphmind.product_server import _request_json, create_product_server
from graphmind.reader import ReaderService
from graphmind.retrieval import AnswerService, RetrievalService
from graphmind.storage import ChromaVectorStore, MemoryGraphStore, MemoryVectorStore, Neo4jGraphStore
from tests.test_graphmind_p2 import make_app
from tests.test_graphmind_p5_p6 import (
    FakeAdminTransport, FakeApplication, FakeAuthority, FakeTransport, identity_settings,
    metadata_document,
)


class AccountFoundationTests(unittest.IsolatedAsyncioTestCase):
    async def test_deleted_disable_enable_requires_restore_and_retains_first_timestamp(self):
        with tempfile.TemporaryDirectory() as temporary:
            transport = FakeAdminTransport()
            admin = KeycloakAdminClient(identity_settings(Path(temporary)), transport=transport)
            await admin.set_state("reader@example.test", "delete")
            timestamp = transport.user["attributes"]["graphmind_deleted_at"]
            await admin.set_state("reader@example.test", "delete")
            self.assertEqual(transport.user["attributes"]["graphmind_deleted_at"], timestamp)
            disabled = await admin.set_state("reader@example.test", "disable")
            self.assertEqual(disabled["status"], "deleted")
            self.assertFalse(disabled["enabled"])
            with self.assertRaises(AccountStateError):
                await admin.set_state("reader@example.test", "enable")
            restored = await admin.set_state("reader@example.test", "restore")
            self.assertEqual(restored["status"], "active")
            self.assertTrue(restored["enabled"])
            self.assertNotIn("graphmind_deleted_at", transport.user["attributes"])

    async def test_deletion_timestamp_alone_prevents_enable(self):
        with tempfile.TemporaryDirectory() as temporary:
            transport = FakeAdminTransport()
            transport.user["attributes"] = {
                "graphmind_status": ["disabled"], "graphmind_deleted_at": ["retained"]
            }
            admin = KeycloakAdminClient(identity_settings(Path(temporary)), transport=transport)
            with self.assertRaises(AccountStateError):
                await admin.set_state("reader@example.test", "enable")
            self.assertFalse(any(method == "PUT" for method, _, _ in transport.calls))

    async def test_missing_persistence_is_not_reported_as_success(self):
        class IgnoringTransport(FakeAdminTransport):
            def request_json(self, method, url, **kwargs):
                if method == "PUT":
                    return {}
                return super().request_json(method, url, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            admin = KeycloakAdminClient(identity_settings(Path(temporary)), transport=IgnoringTransport())
            with self.assertRaisesRegex(AccountStateError, "persist"):
                await admin.set_state("reader@example.test", "delete")

    async def test_concurrent_host_mutation_is_rejected_before_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = identity_settings(Path(temporary))
            transport = FakeAdminTransport()
            admin = KeycloakAdminClient(settings, transport=transport)
            with exclusive_file_lock(settings.data_dir / "account-admin.lock") as acquired:
                self.assertTrue(acquired)
                with self.assertRaises(AccountStateError):
                    await admin.set_state("reader@example.test", "disable")
            self.assertEqual(transport.calls, [])

    async def test_signup_uses_supported_registration_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            authority = KeycloakAuthority(identity_settings(Path(temporary)), transport=FakeTransport())
            url = await authority.authorization_url(redirect_uri="https://rag.example.test/auth/callback",
                                                    state="state", nonce="nonce", code_challenge="challenge",
                                                    signup=True)
            query = parse_qs(urlparse(url).query)
            self.assertEqual(query["prompt"], ["create"])
            self.assertNotIn("kc_action", query)

    def test_profile_is_minimal_and_security_fields_are_admin_only(self):
        root = Path(__file__).resolve().parents[1]
        profile = json.loads((root / "deploy/keycloak/graphmind-user-profile.json").read_text("utf-8"))
        attributes = {attribute["name"]: attribute for attribute in profile["attributes"]}
        # Pinned Keycloak 26.7.3 represents disabled as the omitted/default
        # policy; its REST JSON parser rejects the UI label "DISABLED".
        self.assertNotIn("unmanagedAttributePolicy", profile)
        self.assertEqual({name for name, value in attributes.items() if value.get("required")}, {"email"})
        self.assertNotIn("firstName", attributes)
        self.assertNotIn("lastName", attributes)
        for name in ("graphmind_status", "graphmind_deleted_at", "graphmind_credential_epoch"):
            self.assertEqual(attributes[name]["permissions"], {"view": ["admin"], "edit": ["admin"]})

    def test_realm_resource_audience_is_custom_url_without_client_override(self):
        root = Path(__file__).resolve().parents[1]
        realm = json.loads((root / "deploy/keycloak/graphmind-realm.template.json").read_text("utf-8"))
        scope = next(scope for scope in realm["clientScopes"] if scope["name"] == "graphmind:read")
        audiences = [mapper for mapper in scope["protocolMappers"]
                     if mapper["protocolMapper"] == "oidc-audience-mapper"]
        resource = next(mapper for mapper in audiences
                        if mapper["config"].get("included.custom.audience") == "${GRAPHMIND_IDENTITY_MCP_AUDIENCE}")
        # Keycloak chooses included.client.audience before the custom value;
        # synthetic introspection fixtures must not conceal that precedence.
        self.assertNotIn("included.client.audience", resource["config"])
        self.assertEqual(resource["config"]["access.token.claim"], "true")
        self.assertEqual(resource["config"]["introspection.token.claim"], "true")
        self.assertEqual(resource["config"]["id.token.claim"], "false")
        introspection = next(mapper for mapper in audiences
                             if mapper["config"].get("included.client.audience") == "graphmind-resource")
        self.assertIsNot(introspection, resource)
        self.assertNotIn("included.custom.audience", introspection["config"])
        self.assertEqual(introspection["config"]["access.token.claim"], "true")
        self.assertEqual(introspection["config"]["introspection.token.claim"], "true")
        self.assertEqual(introspection["config"]["id.token.claim"], "false")

    def test_realm_explicitly_defines_required_identity_scopes_and_subject(self):
        root = Path(__file__).resolve().parents[1]
        realm = json.loads((root / "deploy/keycloak/graphmind-realm.template.json").read_text("utf-8"))
        scopes = {scope["name"]: scope for scope in realm["clientScopes"]}
        for client in realm["clients"]:
            self.assertTrue(set(client.get("defaultClientScopes", ())) <= scopes.keys())
        claims = {mapper["config"].get("claim.name"): mapper for mapper in scopes["email"]["protocolMappers"]}
        for name, attribute, kind in (("email", "email", "String"), ("email_verified", "emailVerified", "boolean")):
            config = claims[name]["config"]
            self.assertEqual((config["user.attribute"], config["jsonType.label"]), (attribute, kind))
            for target in ("id.token.claim", "access.token.claim", "introspection.token.claim"):
                self.assertEqual(config[target], "true")
        subject = next(mapper for mapper in scopes["graphmind:read"]["protocolMappers"] if mapper["protocolMapper"] == "oidc-sub-mapper")
        self.assertEqual(subject["config"]["access.token.claim"], "true")
        self.assertEqual(subject["config"]["introspection.token.claim"], "true")


class ModelTransport:
    def __init__(self):
        self.digest = "a" * 64
        self.precision = "F16"
        self.offline = False
        self.change_during_embed = False
        self.calls = []

    def request_json(self, method, url, **kwargs):
        self.calls.append((method, url))
        if self.offline:
            raise HttpUnavailableError()
        if method == "GET":
            return {"models": [{"name": "bge-m3:latest", "digest": self.digest,
                                "details": {"quantization_level": self.precision}}]}
        if self.change_during_embed:
            self.digest = "b" * 64
        return {"embeddings": [[1, 0, 0, 0] for _ in kwargs["payload"]["input"]]}


class EmbeddingFoundationTests(unittest.TestCase):
    def model(self, transport, **kwargs):
        return OllamaEmbedding("http://127.0.0.1:11434", "bge-m3", "BAAI/bge-m3",
                               dimension=4, transport=transport, **kwargs)

    def test_pinned_digest_and_precision_are_both_verified(self):
        transport = ModelTransport()
        with self.assertRaisesRegex(EmbeddingResponseError, "digest"):
            _ = self.model(transport, revision="b" * 64, precision="fp16").fingerprint
        with self.assertRaisesRegex(EmbeddingResponseError, "precision"):
            _ = self.model(transport, revision="a" * 64, precision="fp8").fingerprint
        model = self.model(transport, revision="a" * 64, precision="fp16")
        self.assertTrue(model.readiness().available)
        self.assertTrue(transport.calls)

    def test_cached_identity_does_not_hide_outage_or_tag_change(self):
        transport = ModelTransport()
        model = self.model(transport)
        _ = model.fingerprint
        transport.offline = True
        self.assertFalse(model.readiness().available)
        with self.assertRaises(EmbeddingUnavailableError):
            model.embed("text")
        transport.offline = False
        transport.digest = "b" * 64
        with self.assertRaisesRegex(EmbeddingResponseError, "changed"):
            model.embed("text")
        self.assertFalse(any(method == "POST" for method, _ in transport.calls))

    def test_identity_change_during_embed_discards_all_vectors(self):
        transport = ModelTransport()
        transport.change_during_embed = True
        with self.assertRaisesRegex(EmbeddingResponseError, "changed"):
            self.model(transport).embed_many(["text"])

    def test_missing_runtime_precision_cannot_report_ready(self):
        transport = ModelTransport()
        transport.precision = ""
        self.assertFalse(self.model(transport).readiness().available)


class ReaderFoundationTests(unittest.TestCase):
    def test_fetch_rechecks_tombstone_and_replacement_after_chunk_read(self):
        for change in ("delete", "replace"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "guide.txt"
                source.write_text("cerulean", encoding="utf-8")
                with make_app(root) as app:
                    submission = app.ingestion.prepare_import(source)
                    app.jobs.run_once()
                    original = app.metadata.iter_chunks_for_version

                    def changing_chunks(version_id):
                        chunks = list(original(version_id))
                        if change == "delete":
                            app.ingestion.prepare_delete(submission.document.document_id)
                        else:
                            source.write_text("amber", encoding="utf-8")
                            app.ingestion.prepare_import(source)
                            app.jobs.run_once()
                        yield from chunks

                    app.metadata.iter_chunks_for_version = changing_chunks
                    with self.assertRaises(RetryableQueryError):
                        ReaderService(app).fetch_document(submission.document.document_id)

    def test_fetch_streams_only_necessary_chunks_and_closes_iterator(self):
        app = FakeApplication()
        closed = []

        def chunks(version_id):
            try:
                yield replace(app.metadata.chunks[0], text="x" * 200)
                self.fail("bounded fetch read an unnecessary chunk")
            finally:
                closed.append(True)

        app.metadata.iter_chunks_for_version = chunks
        result = ReaderService(app, max_fetch_chars=30).fetch_document("doc-opaque")
        self.assertEqual(len(result["text"]), 30)
        self.assertTrue(result["truncated"])
        self.assertEqual(closed, [True])

    def test_search_rechecks_sources_after_graph_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "guide.txt"
            source.write_text("cerulean", encoding="utf-8")
            with make_app(root) as app:
                submission = app.ingestion.prepare_import(source)
                app.jobs.run_once()

                def tombstone(*args, **kwargs):
                    app.ingestion.prepare_delete(submission.document.document_id)
                    return []

                app.graph.expand = tombstone
                with self.assertRaises(RetryableQueryError):
                    ReaderService(app).search_documents("cerulean")

    def test_adapter_rechecks_versions_after_authorization(self):
        app = FakeApplication()
        authority = FakeAuthority()
        original = authority.verify_token

        async def revoke_source_on_recheck(token):
            principal = await original(token)
            app.metadata.document_value = replace(app.metadata.document_value, deleted_at="now")
            return principal

        # Fetch's first eligibility check still succeeds; the authorization
        # recheck is the point at which its source is revoked.
        calls = []

        async def verify(token):
            calls.append(token)
            if len(calls) > 1:
                return await revoke_source_on_recheck(token)
            return await original(token)

        authority.verify_token = verify
        with tempfile.TemporaryDirectory() as temporary:
            settings = identity_settings(Path(temporary))
            _, http = create_product_server(settings, app, authority=authority)
            from starlette.testclient import TestClient

            with TestClient(http, base_url=settings.public_base_url) as client:
                client.cookies.set("graphmind_access", "access")
                response = client.get("/documents/doc-opaque")
                self.assertEqual(response.status_code, 409)
                self.assertNotIn("Safe source text", response.text)

    def test_search_and_answer_share_capacity_and_cancelled_search_stops(self):
        entered, release = threading.Event(), threading.Event()
        app = FakeApplication()
        evidence = Evidence("c", "d", "v", "guide", "line 1", "text", "vector", 1)

        def retrieve(query):
            entered.set()
            release.wait(3)
            return [evidence]

        answers = AnswerService(SimpleNamespace(active_version_ids=lambda: {"v"}),
                                SimpleNamespace(retrieve=retrieve), Mock(),
                                max_concurrent_queries=1, max_queued_queries=0, queue_timeout=0)
        app.answers = answers
        reader = ReaderService(app)
        cancelled = threading.Event()
        failures = []

        def search():
            try:
                reader.search_documents("first", cancel_event=cancelled)
            except QueryCancelledError as error:
                failures.append(error.code)

        thread = threading.Thread(target=search)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(answers.query("second").verification["error_code"], "query_capacity_exceeded")
            with self.assertRaises(QueryCapacityError):
                reader.search_documents("third")
            cancelled.set()
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, ["query_cancelled"])


class LeaseFoundationTests(unittest.TestCase):
    def test_lease_transfer_cannot_publish_and_reconciliation_can_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "guide.txt"
            source.write_text("cerulean", encoding="utf-8")
            with make_app(root) as app:
                submission = app.ingestion.prepare_import(source)

                def transfer(point):
                    if point == "before_publish":
                        with app.metadata.transaction() as connection:
                            connection.execute("UPDATE writer_lock SET lease_expires_at = '2000-01-01'")
                            connection.execute("UPDATE jobs SET lease_expires_at = '2000-01-01'")
                        self.assertTrue(app.metadata.acquire_writer("successor", 60))

                app.ingestion.failure_injector = transfer
                with self.assertRaises(JobLeaseError):
                    app.jobs.run_once()
                self.assertIsNone(app.metadata.document(submission.document.document_id).active_version_id)
                self.assertEqual(app.metadata.job(submission.job.job_id).status, JobStatus.RUNNING)
                app.metadata.release_writer("successor")
                self.assertEqual(app.jobs.reconcile(), 1)
                app.ingestion.failure_injector = None
                self.assertEqual(app.jobs.run_once().status, JobStatus.SUCCEEDED)

    def test_lost_writer_does_not_continue_graph_or_delete_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "guide.txt"
            source.write_text("cerulean", encoding="utf-8")
            with make_app(root) as app:
                submission = app.ingestion.prepare_import(source)

                def expire(point):
                    if point == "after_vector":
                        with app.metadata.transaction() as connection:
                            connection.execute("UPDATE writer_lock SET lease_expires_at = '2000-01-01'")

                app.ingestion.failure_injector = expire
                with self.assertRaises(JobLeaseError):
                    app.jobs.run_once()
                self.assertEqual(app.graph.count_version(app.installation_id, submission.version.version_id), 0)
                self.assertFalse(app.metadata.heartbeat(submission.job.job_id, app.jobs.owner, 60))

    def test_stale_attempt_cannot_mutate_reused_owner_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "guide.txt"
            source.write_text("cerulean", encoding="utf-8")
            with make_app(root) as app:
                app.ingestion.prepare_import(source)
                app.metadata.acquire_writer("owner", 60)
                first = app.metadata.claim_next_job("owner", 60)
                with app.metadata.transaction() as connection:
                    connection.execute("UPDATE jobs SET lease_expires_at = '2000-01-01'")
                    connection.execute("UPDATE writer_lock SET lease_expires_at = '2000-01-01'")
                app.metadata.reconcile()
                app.metadata.acquire_writer("owner", 60)
                second = app.metadata.claim_next_job("owner", 60)
                self.assertGreater(second.attempt_count, first.attempt_count)
                with self.assertRaises(JobLeaseError):
                    app.metadata.publish_version(first.version_id, job=first)
                with self.assertRaises(JobLeaseError):
                    app.metadata.finish_job(first.job_id, "owner", succeeded=False, job=first)
                self.assertFalse(app.metadata.heartbeat(first.job_id, "owner", 60, job=first))
                app.metadata.assert_job_lease(second)

    def test_expiry_does_not_allow_second_executor_to_overlap_inflight_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "guide.txt"
            source.write_text("cerulean", encoding="utf-8")
            with make_app(root) as app:
                app.ingestion.prepare_import(source)
                entered, release = threading.Event(), threading.Event()

                def handler(job):
                    with app.metadata.transaction() as connection:
                        connection.execute("UPDATE writer_lock SET lease_expires_at = '2000-01-01'")
                    entered.set()
                    release.wait(3)

                first = DurableJobExecutor(app.metadata, handler, lease_seconds=60, owner="first")
                second = DurableJobExecutor(app.metadata, Mock(), lease_seconds=60, owner="second")
                errors = []

                def run():
                    try:
                        first.run_once()
                    except JobLeaseError:
                        errors.append(True)

                thread = threading.Thread(target=run)
                thread.start()
                try:
                    self.assertTrue(entered.wait(2))
                    with self.assertRaises(JobLeaseError):
                        second.run_once()
                    with self.assertRaises(JobLeaseError):
                        second.reconcile()
                    second.handler.assert_not_called()
                finally:
                    release.set()
                    thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [True])

    def test_kernel_lock_blocks_separate_process_and_releases(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "writer.lock"
            code = ("import sys; from pathlib import Path; "
                    "from graphmind.locking import exclusive_file_lock\n"
                    "with exclusive_file_lock(Path(sys.argv[1])) as acquired:\n"
                    " sys.exit(0 if acquired else 3)\n")
            with exclusive_file_lock(path) as acquired:
                self.assertTrue(acquired)
                self.assertEqual(subprocess.run([sys.executable, "-B", "-c", code, str(path)],
                                               timeout=10, check=False).returncode, 3)
            self.assertEqual(subprocess.run([sys.executable, "-B", "-c", code, str(path)],
                                           timeout=10, check=False).returncode, 0)


class StoreFoundationTests(unittest.TestCase):
    def test_historical_vectors_cannot_starve_active_evidence(self):
        embedding = HashEmbedding(64)
        vector = MemoryVectorStore(embedding)
        active = Chunk("current", "current", "doc", 0, "cerulean current", "line 1", 0, 16)
        history = [Chunk(f"old-{i}", "old", "doc", i, "cerulean", "line 1", 0, 8) for i in range(100)]
        vector.upsert_version("install", history + [active])
        document = Document("doc", "default", "guide", "guide", "text/plain", "source", "now", "current")
        metadata = SimpleNamespace(active_embedding_fingerprints=lambda: {embedding.fingerprint},
                                   active_version_ids=lambda: {"current"},
                                   active_chunks=lambda ids: {"current": (active, document)} if "current" in ids else {})
        evidence = RetrievalService(metadata, vector, MemoryGraphStore(), "install").retrieve("cerulean")
        self.assertEqual([item.citation_id for item in evidence], ["current"])

    def test_graph_filters_target_versions_before_limit(self):
        graph = MemoryGraphStore()
        document, version, chunks = metadata_document()
        graph.upsert_version("install", document, version, chunks)
        seed = Chunk("seed", "source", "source", 0, "link", "line 1", 0, 4, (document.normalized_name,))
        graph._chunks[("install", "seed")] = seed
        for number in range(100):
            chunk = replace(chunks[0], chunk_id=f"000-old-{number}", version_id="old")
            graph._chunks[("install", chunk.chunk_id)] = chunk
        self.assertEqual(graph.expand("install", ["seed"], 1,
                                      active_version_ids={"source", version.version_id}), [chunks[0].chunk_id])

    def test_chroma_query_filters_versions_in_backend(self):
        store = ChromaVectorStore("unused", "unused", HashEmbedding(64))
        store._collection = Mock()
        store._collection.query.return_value = {"ids": [["current"]], "distances": [[0.1]]}
        store.search("install", "cerulean", 1, active_version_ids={"active"})
        self.assertEqual(store._collection.query.call_args.kwargs["where"],
                         {"$and": [{"installation_id": {"$eq": "install"}},
                                   {"version_id": {"$in": ["active"]}}]})
        store._collection.query.reset_mock()
        self.assertEqual(store.search("install", "cerulean", 4, active_version_ids=set()), [])
        store._collection.query.assert_not_called()

    def test_real_chroma_active_filter_prevents_history_starvation(self):
        # Separate process releases Chroma's Windows file handles at exit.
        code = """
import sys
from graphmind.domain import Chunk
from graphmind.embeddings import HashEmbedding
from graphmind.storage import ChromaVectorStore
store = ChromaVectorStore(sys.argv[1], 'gm_foundation_filter', HashEmbedding(64))
history = [Chunk(f'old-{i}', 'old', 'doc', i, 'cerulean', 'line 1', 0, 8) for i in range(100)]
active = Chunk('current', 'active', 'doc', 0, 'cerulean current', 'line 1', 0, 16)
store.upsert_version('installation', history + [active])
hits = store.search('installation', 'cerulean', 4, active_version_ids={'active'})
assert [hit.chunk_id for hit in hits] == ['current'], hits
assert store.search('other-installation', 'cerulean', 4, active_version_ids={'active'}) == []
assert store.search('installation', 'cerulean', 4, active_version_ids=set()) == []
store.close()
"""
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run([sys.executable, "-B", "-c", code, temporary],
                                    capture_output=True, text=True, timeout=30, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_chroma_short_result_fallback_is_paged_and_uses_cosine_scores(self):
        embedding = HashEmbedding(64)
        query = embedding.embed("cerulean")
        store = ChromaVectorStore("unused", "unused", embedding)
        store._collection = Mock()
        store._collection.query.return_value = {"ids": [[]], "distances": [[]]}
        store._collection.get.side_effect = [
            {"ids": [f"first-{index}" for index in range(256)], "embeddings": [[0.0] * 64] * 256},
            {"ids": ["best"], "embeddings": [[value * 7 for value in query]]},
        ]
        hits = store.search("install", "cerulean", 1, active_version_ids={"active"})
        self.assertEqual([hit.chunk_id for hit in hits], ["best"])
        self.assertAlmostEqual(hits[0].score, 1)
        self.assertEqual([call.kwargs["offset"] for call in store._collection.get.call_args_list], [0, 256])
        self.assertTrue(all(call.kwargs["limit"] == 256 for call in store._collection.get.call_args_list))

    def test_neo4j_upsert_is_one_transaction_and_references_are_scoped(self):
        document, version, chunks = metadata_document()
        store = Neo4jGraphStore("unused", "unused", "unused")
        driver = Mock()
        session = driver.session.return_value.__enter__ = Mock(return_value=Mock())
        driver.session.return_value.__exit__ = Mock(return_value=False)
        transaction = Mock()
        session.return_value.execute_write.side_effect = lambda callback: callback(transaction)
        store._driver = driver
        store.upsert_version("install", document, version, chunks)
        session.return_value.execute_write.assert_called_once()
        self.assertEqual(transaction.run.call_count, 3)
        driver.execute_query.assert_not_called()
        for call in transaction.run.call_args_list[1:]:
            self.assertIn("source.version_id = $version_id OR target.document_id = $document_id", call.args[0])

        transaction.run.side_effect = [Mock(), Mock(), RuntimeError("resolution failed")]
        with self.assertRaises(DependencyUnavailableError):
            store.upsert_version("install", document, version, chunks)
        # The exception escapes the transaction callback for driver rollback.

    def test_neo4j_expansion_filters_source_and_target_before_limit(self):
        store = Neo4jGraphStore("unused", "unused", "unused")
        store._driver = Mock()
        store._driver.execute_query.return_value = ([{"chunk_id": "current"}], None, None)
        for scope in ("chunk", "document"):
            self.assertEqual(store.expand("install", ["seed"], 1, scope,
                                          active_version_ids={"source", "active"}), ["current"])
            call = store._driver.execute_query.call_args
            self.assertLess(call.args[0].index("chunk.version_id IN $active_versions"),
                            call.args[0].index("LIMIT $limit"))
            self.assertIn("source.version_id IN $active_versions", call.args[0])
            self.assertEqual(call.kwargs["active_versions"], ["active", "source"])


class InputFoundationTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunked_body_stops_reading_at_limit_without_buffering_whole_stream(self):
        consumed = []

        async def stream():
            for chunk in (b"x" * 700, b"x" * 700, b"must not be read"):
                consumed.append(len(chunk))
                yield chunk

        with tempfile.TemporaryDirectory() as temporary:
            settings = replace(identity_settings(Path(temporary)), browser_max_request_bytes=1024)
            request = SimpleNamespace(headers={}, stream=stream)
            with self.assertRaisesRegex(ValueError, "large"):
                await _request_json(request, settings)
            self.assertEqual(consumed, [700, 700])

    def test_source_read_is_bounded_before_parser(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "guide.txt"
            source.write_text("cerulean", encoding="utf-8")
            with make_app(root) as app:
                app.ingestion.settings = replace(app.settings, max_file_bytes=4)
                stream = Mock()
                stream.read.return_value = b"12345"
                stream.__enter__ = Mock(return_value=stream)
                stream.__exit__ = Mock(return_value=False)
                with patch.object(Path, "open", return_value=stream), self.assertRaises(DocumentTooLargeError):
                    app.ingestion.prepare_import(source)
                stream.read.assert_called_once_with(5)


class AdapterCapacityTests(unittest.IsolatedAsyncioTestCase):
    def request(self, settings, *, disconnect=None, question="first"):
        from starlette.requests import Request

        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": json.dumps({"question": question}).encode(),
                        "more_body": False}
            if disconnect is not None and disconnect.is_set():
                return {"type": "http.disconnect"}
            await asyncio.sleep(1000)

        return Request({"type": "http", "method": "POST", "path": "/api/ask",
                        "query_string": b"", "scheme": "https",
                        "headers": [(b"origin", settings.public_base_url.encode()),
                                    (b"x-graphmind-csrf", b"test-csrf"),
                                    (b"cookie", b"graphmind_access=access; graphmind_csrf=test-csrf")]},
                       receive=receive)

    async def exercise(self, *, disconnect_client):
        with tempfile.TemporaryDirectory() as temporary:
            settings = replace(identity_settings(Path(temporary)), max_concurrent_queries=1,
                               max_queued_queries=0)
            app = FakeApplication()
            original = app.answers.query
            entered, release, finished = threading.Event(), threading.Event(), threading.Event()
            signals = []

            def query(question, *, cancel_event):
                signals.append(cancel_event)
                entered.set()
                try:
                    release.wait(3)
                    return original(question)
                finally:
                    finished.set()

            app.answers.query = query
            _, http = create_product_server(settings, app, authority=FakeAuthority())
            endpoint = next(route.endpoint for route in http.app.routes if getattr(route, "path", "") == "/api/ask")
            disconnect = threading.Event()
            first = asyncio.create_task(endpoint(self.request(settings, disconnect=disconnect)))
            try:
                for _ in range(200):
                    if entered.is_set():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(entered.is_set())
                if disconnect_client:
                    disconnect.set()
                    for _ in range(200):
                        if signals[0].is_set():
                            break
                        await asyncio.sleep(0.01)
                    self.assertTrue(signals[0].is_set())
                else:
                    first.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await first
                    self.assertTrue(signals[0].is_set())
                second = await endpoint(self.request(settings, question="second"))
                self.assertEqual(second.status_code, 503)
                self.assertEqual(len(signals), 1)  # Rejected before executor submission.
            finally:
                release.set()
                if not first.cancelled():
                    await first
                for _ in range(200):
                    if finished.is_set():
                        break
                    await asyncio.sleep(0.01)
                # Let the completed worker's finally release its slot.
                await asyncio.sleep(0.01)
            third = await endpoint(self.request(settings, question="third"))
            self.assertEqual(third.status_code, 200)
            self.assertEqual(len(signals), 2)

    async def test_cancelled_adapter_keeps_slot_until_worker_finishes(self):
        await self.exercise(disconnect_client=False)

    async def test_browser_disconnect_signals_running_work(self):
        await self.exercise(disconnect_client=True)


if __name__ == "__main__":
    unittest.main()
