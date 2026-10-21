"""Shared local model transport and resource admission contracts."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from graphmind.errors import ProviderResponseError, QueryCapacityError
from graphmind.locking import exclusive_file_lock
from graphmind.providers import OllamaProvider
from graphmind.rag.components.local_inference import LocalInference
from graphmind.rag.components.embeddings import OllamaEmbedding


class LocalModelAdmissionTests(unittest.TestCase):
    def test_tree_navigation_uses_the_existing_provider_and_rejects_invented_nodes(self):
        class Transport:
            content = '{"node_ids":["section1"]}'
            requests = []
            def request_json(self, method, url, **options):
                self.requests.append((method, url, options["payload"]))
                return {"message": {"content": self.content}}
        transport = Transport()
        provider = OllamaProvider("http://127.0.0.1:21434", "", "gemma4:e2b", mode="local",
                                  max_retries=0, transport=transport)
        candidates = [{"node_id": "section1", "title": "Maintenance", "preview": "Original page text"}]
        self.assertEqual(provider.select_nodes("What is the interval?", candidates), ["section1"])
        self.assertEqual(transport.requests[0][2]["format"]["properties"]["node_ids"]["maxItems"], 2)
        self.assertIs(transport.requests[0][2]["think"], False)
        transport.content = '{"node_ids":["invented"]}'
        with self.assertRaises(ProviderResponseError):
            provider.select_nodes("What is the interval?", candidates)
        self.assertEqual(provider.call_count, 2)

    def test_local_chat_and_workers_share_admission_and_release_ollama_weights(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inference = LocalInference(root)
            class Transport:
                post_calls = 0
                def request_json(self, method, url, **options):
                    if method == "GET":
                        return {"models": [{"name": "bge-m3:latest", "digest": "a" * 64,
                                            "details": {"quantization_level": "F16"}}]}
                    self.post_calls += 1
                    self.body = options["payload"]
                    if url.endswith("/api/embed"):
                        return {"embeddings": [[1.0] * 64]}
                    return {"message": {"content": '{"node_ids":["section1"]}'}}
            transport = Transport()
            provider = OllamaProvider("http://127.0.0.1:21434", "", "gemma4:e2b", mode="local",
                max_retries=0, transport=transport, inference_lock=inference.lock_path)
            embedding = OllamaEmbedding("http://127.0.0.1:21434", "bge-m3", "BAAI/bge-m3",
                                        dimension=64, transport=transport)
            embedding.inference_lock = inference.lock_path
            with exclusive_file_lock(inference.lock_path) as acquired:
                self.assertTrue(acquired)
                with self.assertRaises(QueryCapacityError):
                    provider.select_nodes("Interval?", [{"node_id": "section1"}])
                with self.assertRaises(QueryCapacityError):
                    embedding.embed("Original source text")
                with patch("graphmind.rag.components.local_inference.subprocess.run") as worker:
                    with self.assertRaises(QueryCapacityError):
                        inference.run("tree", {"pdf_path": "unused.pdf"})
                    worker.assert_not_called()
            self.assertEqual(provider.call_count, 0)
            self.assertEqual(transport.post_calls, 0)
            self.assertEqual(len(embedding.embed("Original source text")), 64)
            self.assertEqual(transport.body["keep_alive"], "0")
            provider.select_nodes("Interval?", [{"node_id": "section1"}])
            self.assertEqual(transport.body["keep_alive"], "0")
