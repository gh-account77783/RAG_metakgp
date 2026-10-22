"""Single-owner HTTP selection is explicit, loopback-only and bounded."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from graphmind.config import Settings
from graphmind.embeddings import HashEmbedding
from graphmind.errors import ConfigurationError
from graphmind.rag.components.chroma_http import BoundedChromaHTTP, ChromaHTTPClient, FastAPI
from graphmind.rag.components.vector_store import ChromaVectorStore
from graphmind.service import build_vector_store


class ChromaHTTPTests(unittest.TestCase):
    def test_environment_selection_reuses_one_server_with_distinct_collections(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = Settings.load(dotenv_path=root / 'missing.env', environ={
                'GRAPHMIND_CHROMA_MODE': 'http', 'GRAPHMIND_CHROMA_PORT': '21801'},
                explicit={'data_dir': root / 'data', 'allowed_import_roots': (root,)})
            embedding = HashEmbedding(64)
            legacy = build_vector_store(settings, embedding)
            hybrid = build_vector_store(settings, embedding, hybrid=True)
            self.assertEqual((legacy.host, legacy.port), ('127.0.0.1', 21801))
            self.assertEqual((hybrid.host, hybrid.port), (legacy.host, legacy.port))
            self.assertNotEqual(legacy.collection_name, hybrid.collection_name)
            for invalid in ({'chroma_mode': 'cloud'}, {'chroma_host': 'remote.example'}, {'chroma_port': 0}):
                with self.assertRaises(ConfigurationError):
                    Settings(data_dir=root, allowed_import_roots=(root,), **invalid).validate()
            with self.assertRaises(ConfigurationError):
                ChromaHTTPClient('remote.example', 21801)

    def test_all_http_dispatch_including_initialization_has_a_timeout(self):
        client = object.__new__(BoundedChromaHTTP)
        with patch.object(FastAPI, '_make_request', return_value={'ok': True}) as upstream:
            self.assertEqual(client._make_request('GET', '/auth/identity'), {'ok': True})
            self.assertEqual(upstream.call_args.kwargs['timeout'], 30.0)
            client._make_request('POST', '/collections', timeout=None, json={'name': 'owned'})
            self.assertEqual(upstream.call_args.kwargs['timeout'], 30.0)

    def test_unavailable_http_server_does_not_open_an_embedded_store(self):
        import chromadb
        store = ChromaVectorStore('unused', 'owned_http', HashEmbedding(64), host='127.0.0.1', port=21801)
        with patch('graphmind.rag.components.chroma_http.ChromaHTTPClient', side_effect=RuntimeError('owned outage')), \
                patch.object(chromadb, 'PersistentClient') as embedded:
            self.assertFalse(store.readiness().available)
            embedded.assert_not_called()
