"""Shared browser/MCP catalogue and original-page source protections."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient
from graphmind.embeddings import HashEmbedding
from graphmind.domain import AdapterStatus
from graphmind.product_server import create_product_server, ACCESS_COOKIE, CSRF_COOKIE
from graphmind.reader import ReaderService
from graphmind.service import GraphMindApplication
from graphmind.storage import MemoryGraphStore, MemoryVectorStore
from tests.engines.test_tree_navigation import FixtureNavigator
from tests.engines.test_visual_retrieval import FixtureVisualAdapter
from tests.support.identity import FakeAuthority, identity_settings
from tests.support.pdf_engine_documents import documents


class ReaderSourceTests(unittest.TestCase):
    def test_catalogue_download_and_page_use_current_sources_and_existing_authorization(self):
        from dataclasses import replace
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = replace(identity_settings(root), enabled_engines=("tree_navigation", "multimodal"))
            source = root / "owned Ω document.pdf"
            original = documents()["tree.pdf"]
            source.write_bytes(original)
            class FixtureImageProvider(FixtureNavigator):
                def image_readiness(self):
                    return AdapterStatus("fixture-image-input", True, "ready")
            with GraphMindApplication(settings, vector=MemoryVectorStore(HashEmbedding(64)),
                    graph=MemoryGraphStore(), provider=FixtureImageProvider(),
                    visual_adapter=FixtureVisualAdapter()) as application:
                submitted = application.ingestion.prepare_import(source, engines=("tree_navigation",))
                application.jobs.run_job(submitted.job.job_id)
                identifier, version = submitted.document.document_id, submitted.version.version_id
                reader = ReaderService(application)
                catalog = reader.catalog()
                self.assertTrue(catalog["documents"][0]["engines"]["tree_navigation"]["ready"])
                self.assertEqual(catalog["documents"][0]["engines"]["multimodal"]["state"], "not_indexed")
                self.assertNotIn("private_file_ref", str(catalog))
                self.assertNotIn(str(root), str(catalog))
                authority = FakeAuthority()
                _, asgi = create_product_server(settings, application, authority=authority)
                target = f"/documents/{identifier}"
                with TestClient(asgi, base_url=settings.public_base_url) as client:
                    with patch.object(application.files, "read_bytes", wraps=application.files.read_bytes) as read:
                        self.assertEqual(client.get(target + "/original").status_code, 401)
                        read.assert_not_called()
                    client.cookies.set(ACCESS_COOKIE, "access")
                    client.cookies.set(CSRF_COOKIE, "fixture-csrf")
                    headers = {"Origin": settings.public_base_url, "X-GraphMind-CSRF": "fixture-csrf"}
                    catalog_response = client.post("/api/catalog", json={}, headers=headers)
                    self.assertEqual(catalog_response.status_code, 200, catalog_response.text)
                    downloaded = client.get(target + "/original", params={"version_id": version})
                    self.assertEqual(downloaded.content, original)
                    self.assertEqual(downloaded.headers["content-type"], "application/octet-stream")
                    self.assertTrue(downloaded.headers["content-disposition"].startswith("attachment;"))
                    self.assertIn("%CE%A9", downloaded.headers["content-disposition"])
                    page = client.get(target + "/pages/2", params={"version_id": version})
                    self.assertEqual(page.status_code, 200, page.text[:100] if page.status_code != 200 else "")
                    self.assertTrue(page.content.startswith(b"\x89PNG"))
                    self.assertEqual(page.headers["cache-control"], "no-store")
                    self.assertEqual(client.get(target + "/pages/99").status_code, 400)
                    self.assertEqual(client.get(target + "/original", params={"version_id": "stale"}).status_code, 404)
                    mcp = client.post("/mcp", headers={"Accept": "application/json, text/event-stream",
                        "Authorization": "Bearer access", "Mcp-Protocol-Version": "2025-11-25"},
                        json={"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {
                            "uri": f"graphmind://documents/{identifier}/versions/{version}/pages/2"}})
                    self.assertEqual(mcp.status_code, 200, mcp.text)
                    self.assertEqual(mcp.json()["result"]["contents"][0]["mimeType"], "image/png")
                    self.assertIn("blob", mcp.json()["result"]["contents"][0])
                    # Revocation after the original is read must discard the bytes.
                    read_original = application.files.read_bytes
                    def revoke_after_read(*args, **kwargs):
                        content = read_original(*args, **kwargs)
                        authority.valid_tokens.clear()
                        return content
                    with patch.object(application.files, "read_bytes", side_effect=revoke_after_read):
                        denied = client.get(target + "/original")
                    self.assertEqual(denied.status_code, 401)
                    self.assertEqual(denied.json()["error"], "authorization_revoked")
                    self.assertNotIn(original, denied.content)
                deletion = application.ingestion.prepare_delete(identifier)
                self.assertEqual(reader.catalog()["documents"], [])
