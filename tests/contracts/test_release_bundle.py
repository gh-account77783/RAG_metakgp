"""Offline bundle integrity and dependency-closure contracts, using synthetic wheels."""
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from tools.release_bundle import freeze, verify


class ReleaseBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.bundle = Path(self.temporary.name)
        (self.bundle / "wheelhouse").mkdir()

    def wheel(self, name, *, version="1.0", dependencies=(), extras=(), tag="py3-none-any"):
        path = self.bundle / "wheelhouse" / f"{name}-{version}-{tag}.whl"
        directory = f"{name}-{version}.dist-info"
        metadata = f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\nRequires-Python: >=3.13\n"
        metadata += "".join(f"Requires-Dist: {value}\n" for value in dependencies)
        metadata += "".join(f"Provides-Extra: {value}\n" for value in extras)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(directory + "/METADATA", metadata)
            archive.writestr(directory + "/licenses/LICENSE", b"synthetic retained notice\n")
        return path

    def project(self, **kwargs):
        return self.wheel("graphmind_rag_mcp", **kwargs)

    def test_freeze_complete_marked_extra_closure_and_preserve_notices(self):
        self.project(dependencies=('base>=1', 'optional[detail]>=1; extra == "tree"',
                                   'absent>=1; python_version < "3"'), extras=("tree",))
        self.wheel("base")
        self.wheel("optional", dependencies=('leaf==1.0; extra == "detail"',), extras=("detail",))
        self.wheel("leaf")
        manifest = freeze(self.bundle, extras=["tree"])
        self.assertEqual(len(manifest["wheels"]), 4)
        self.assertEqual(verify(self.bundle), manifest)
        lock = (self.bundle / "requirements.lock").read_text()
        self.assertEqual(lock.count("--hash=sha256:"), 4)
        self.assertEqual(len(manifest["notices"]), 4)
        self.assertEqual(manifest["rights_disposition"], "requires review")
        for relative in manifest["notices"]:
            self.assertEqual((self.bundle / relative).read_bytes(), b"synthetic retained notice\n")

    def test_missing_transitive_extra_dependency_prevents_freeze(self):
        self.project(dependencies=('optional[detail]>=1; extra == "tree"',), extras=("tree",))
        self.wheel("optional", dependencies=('missing>=1; extra == "detail"',), extras=("detail",))
        with self.assertRaisesRegex(ValueError, "Missing or incompatible"):
            freeze(self.bundle, extras=["tree"])
        self.assertFalse((self.bundle / "manifest.json").exists())

    def test_incompatible_dependency_or_unknown_extra_is_rejected(self):
        self.project(dependencies=("base>=2",))
        self.wheel("base")
        with self.assertRaises(ValueError):
            freeze(self.bundle)
        with self.assertRaisesRegex(ValueError, "extra is unavailable"):
            freeze(self.bundle, extras=["nonexistent"])

    def test_changed_wheel_missing_wheel_or_extra_artifact_is_rejected(self):
        project = self.project()
        freeze(self.bundle)
        original = project.read_bytes()
        project.write_bytes(original + b"tampered")
        with self.assertRaisesRegex(ValueError, "bytes changed"):
            verify(self.bundle)
        project.write_bytes(original)
        (self.bundle / "wheelhouse" / "unexpected.txt").write_text("unexpected")
        with self.assertRaisesRegex(ValueError, "wheels only"):
            verify(self.bundle)
        (self.bundle / "wheelhouse" / "unexpected.txt").unlink()
        project.unlink()
        with self.assertRaises(ValueError):
            verify(self.bundle)

    def test_lock_and_notice_changes_are_rejected(self):
        self.project()
        manifest = freeze(self.bundle)
        lock = self.bundle / "requirements.lock"
        original = lock.read_bytes()
        lock.write_bytes(original + b"unexpected==1\n")
        with self.assertRaisesRegex(ValueError, "lock differs"):
            verify(self.bundle)
        lock.write_bytes(original)
        notice = self.bundle / next(iter(manifest["notices"]))
        notice.write_bytes(b"changed notice")
        with self.assertRaisesRegex(ValueError, "notice bytes"):
            verify(self.bundle)

    def test_duplicate_versions_incompatible_tags_and_urls_are_rejected(self):
        self.project()
        duplicate = self.project(version="2.0")
        with self.assertRaisesRegex(ValueError, "one artifact"):
            freeze(self.bundle)
        duplicate.unlink()
        foreign = self.wheel("foreign", tag="cp39-cp39-any")
        with self.assertRaisesRegex(ValueError, "incompatible"):
            freeze(self.bundle)
        foreign.unlink()
        project = next((self.bundle / "wheelhouse").iterdir())
        project.unlink()
        self.project(dependencies=("remote @ https://example.invalid/package.whl",))
        with self.assertRaisesRegex(ValueError, "Direct-URL"):
            freeze(self.bundle)

    def test_existing_bundle_and_foreign_target_are_rejected(self):
        self.project()
        freeze(self.bundle)
        with self.assertRaisesRegex(ValueError, "fresh directory"):
            freeze(self.bundle)
        path = self.bundle / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["target"]["platform"] = "foreign-target"
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "native target"):
            verify(self.bundle)

    def test_unsafe_upstream_notice_path_cannot_escape_bundle(self):
        path = self.project()
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr("graphmind_rag_mcp-1.0.dist-info/licenses/../../escaped", "unsafe")
        with self.assertRaisesRegex(ValueError, "Unsafe notice"):
            freeze(self.bundle)
        self.assertFalse((self.bundle / "manifest.json").exists())

    def test_vendored_distribution_metadata_is_not_a_second_installable_package(self):
        path = self.project()
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr("package/_vendor/dependency-2.0.dist-info/METADATA",
                             "Metadata-Version: 2.4\nName: dependency\nVersion: 2.0\n")
            archive.writestr("package/_vendor/dependency-2.0.dist-info/licenses/NOTICE", "retained vendor notice")
        manifest = freeze(self.bundle)
        self.assertEqual(len(manifest["wheels"]), 1)
        self.assertEqual(len(manifest["notices"]), 2)
        self.assertEqual(verify(self.bundle), manifest)
