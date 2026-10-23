"""Offline bundle integrity and dependency-closure contracts, using synthetic wheels."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from tools.release_bundle import collect, freeze, install, verify


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
            archive.writestr(directory + "/WHEEL", f"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: {tag}\n")
            archive.writestr(directory + "/RECORD", "")
            archive.writestr(directory + "/licenses/LICENSE", b"synthetic retained notice\n")
        return path

    def test_collect_local_wheel_then_install_fresh_offline_environment(self):
        wheel = self.project()
        candidate = self.bundle / "candidate"
        parent = self.bundle / "parent-import-path"
        metadata = parent / "unrelated-1.0.dist-info"
        metadata.mkdir(parents=True)
        (metadata / "METADATA").write_text(
            "Metadata-Version: 2.4\nName: unrelated\nVersion: 1.0\nRequires-Dist: absent-package==1.0\n")
        polluted = {"PYTHONPATH": str(parent), "PYTHONHOME": str(self.bundle / "not-a-python-home")}
        with patch.dict(os.environ, polluted):
            manifest = collect(candidate, wheel)
        self.assertEqual(manifest["provenance"]["project_sha256"], manifest["wheels"][0]["sha256"])
        environment = self.bundle / "isolated"
        with patch.dict(os.environ, polluted):
            receipt = install(candidate, environment)
        self.assertTrue(receipt["offline_install"])
        self.assertTrue(receipt["pip_check"])
        executable = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        result = subprocess.run([str(executable), "-I", "-c",
                                 "import importlib.metadata; print(importlib.metadata.version('graphmind-rag-mcp'))"],
                                check=True, capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "1.0")
        self.assertEqual(json.loads((environment / "bundle-install.json").read_text()), receipt)
        with self.assertRaises(FileExistsError):
            collect(candidate, wheel)
        with self.assertRaisesRegex(ValueError, "new environment"):
            install(candidate, environment)

    def test_failed_collection_retains_log_without_completion_and_cannot_be_reused(self):
        wheel = self.project()
        candidate = self.bundle / "failed-candidate"
        with patch("tools.release_bundle.subprocess.run", side_effect=subprocess.CalledProcessError(1, "pip")):
            with self.assertRaises(subprocess.CalledProcessError):
                collect(candidate, wheel)
        self.assertTrue((candidate / "collection.log").is_file())
        self.assertFalse((candidate / "manifest.json").exists())
        with self.assertRaises(FileExistsError):
            collect(candidate, wheel)

    def test_install_rejects_tampered_bundle_before_creating_environment(self):
        wheel = self.project()
        freeze(self.bundle)
        wheel.write_bytes(wheel.read_bytes() + b"changed")
        environment = self.bundle.parent / (self.bundle.name + "-env")
        with self.assertRaisesRegex(ValueError, "bytes changed"):
            install(self.bundle, environment)
        self.assertFalse(environment.exists())

    def test_freeze_rejects_revoked_distribution_before_approving_bundle(self):
        self.project()
        self.wheel('litellm', version='1.107.0')
        with self.assertRaisesRegex(ValueError, 'Revoked compromised distribution'):
            freeze(self.bundle)
        self.assertFalse((self.bundle / 'manifest.json').exists())

    def test_failed_install_retains_environment_without_success_receipt(self):
        self.project()
        freeze(self.bundle)
        environment = self.bundle / "new-environment"
        # A bundle cannot contain its own mutable installation.
        with self.assertRaisesRegex(ValueError, "outside the bundle"):
            install(self.bundle, environment)
        candidate = self.bundle / "candidate"
        collect(candidate, next((self.bundle / "wheelhouse").glob("*.whl")))
        with patch("tools.bundle_installer.venv.EnvBuilder"), patch(
                "tools.bundle_installer.subprocess.run", side_effect=subprocess.CalledProcessError(1, "pip")):
            with self.assertRaises(subprocess.CalledProcessError):
                install(candidate, environment)
        self.assertTrue((environment / "bundle-install.log").exists())
        self.assertFalse((environment / "bundle-install.json").exists())

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
