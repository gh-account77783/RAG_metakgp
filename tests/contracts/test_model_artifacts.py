"""Pinned model artifact collection without network access or inference."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.model_artifacts import MANIFEST, allowed_file, collect, verify


class ModelArtifactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.destination = Path(temporary.name) / "model"
        self.model, self.revision = "owner/example", "a" * 40
        self.contents = {"config.json": b'{"architectures": []}', "model.safetensors": b"synthetic weights",
                         "README.md": b"synthetic terms", "unsafe.py": b"must not download"}
        siblings = []
        for name, content in self.contents.items():
            item = {"rfilename": name, "size": len(content),
                    "blobId": hashlib.sha1(f"blob {len(content)}\0".encode() + content,
                                           usedforsecurity=False).hexdigest()}
            if name.endswith(".safetensors"):
                item["lfs"] = {"sha256": hashlib.sha256(content).hexdigest()}
            siblings.append(item)
        self.metadata = {"sha": self.revision, "private": False, "gated": False,
                         "cardData": {"license": "example-only"}, "siblings": siblings}
        self.calls = []
        self.patch = patch("tools.model_artifacts.urllib.request.urlopen", side_effect=self.response)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        # Tiny offline fixtures must not depend on the host's available disk.
        capacity = patch("tools.model_artifacts.shutil.disk_usage",
                         return_value=SimpleNamespace(free=8 * 1024**3))
        capacity.start()
        self.addCleanup(capacity.stop)

    def response(self, url, **kwargs):
        self.calls.append(url)
        if "/api/models/" in url:
            return io.BytesIO(json.dumps(self.metadata).encode())
        return io.BytesIO(self.contents[url.rsplit("/", 1)[-1]])

    def collect(self, **kwargs):
        return collect(self.destination, self.model, self.revision, **kwargs)

    def test_collect_pinned_assets_verify_both_identity_forms_and_retain_terms(self):
        manifest = self.collect()
        self.assertEqual(verify(self.destination), manifest)
        self.assertEqual(manifest["declared_license"], "example-only")
        self.assertEqual(manifest["rights_disposition"], "requires review")
        self.assertEqual({item["filename"] for item in manifest["files"]},
                         {"config.json", "model.safetensors", "README.md"})
        self.assertTrue(all(self.revision in url and url.startswith("https://huggingface.co/")
                            for url in self.calls))
        before = len(self.calls)
        with self.assertRaisesRegex(ValueError, "new destination"):
            self.collect()
        self.assertEqual(before, len(self.calls))

    def test_lfs_mismatch_leaves_partial_without_completion(self):
        self.metadata["siblings"][1]["lfs"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            self.collect()
        self.assertFalse((self.destination / MANIFEST).exists())
        self.assertTrue((self.destination / "model.safetensors.part").exists())

    def test_git_blob_mismatch_prevents_completion(self):
        self.metadata["siblings"][0]["blobId"] = "0" * 40
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            self.collect()
        self.assertFalse((self.destination / MANIFEST).exists())

    def test_size_limit_and_changed_revision_fail_before_creating_destination(self):
        with self.assertRaisesRegex(ValueError, "download size"):
            self.collect(max_bytes=1)
        self.assertFalse(self.destination.exists())
        with patch("tools.model_artifacts.shutil.disk_usage", return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(ValueError, "Insufficient free disk"):
                self.collect()
        self.assertFalse(self.destination.exists())
        self.metadata["sha"] = "b" * 40
        with self.assertRaisesRegex(ValueError, "requested public"):
            self.collect()
        self.assertFalse(self.destination.exists())
        self.assertEqual(len(self.calls), 3)
        with self.assertRaisesRegex(ValueError, "exact commit"):
            collect(self.destination, self.model, "main")
        self.assertEqual(len(self.calls), 3)

    def test_verification_rejects_changed_missing_or_added_assets(self):
        self.collect()
        path = self.destination / "config.json"
        original = path.read_bytes()
        path.write_bytes(b"x" * len(original))
        with self.assertRaisesRegex(ValueError, "bytes changed"):
            verify(self.destination)
        path.write_bytes(original)
        added = self.destination / "extra.py"
        added.write_bytes(b"unapproved")
        with self.assertRaisesRegex(ValueError, "inventory changed"):
            verify(self.destination)
        added.unlink()
        path.unlink()
        with self.assertRaisesRegex(ValueError, "inventory changed"):
            verify(self.destination)

    def test_executable_pickle_nested_and_unsafe_names_are_excluded(self):
        for name in ("model.bin", "model.pkl", "weights.pt", "code.py", "README.py", "LICENSE.bin",
                     "../config.json", "nested/config.json", "C:config.json", ".hidden.json"):
            with self.subTest(name=name):
                self.assertFalse(allowed_file(name))
