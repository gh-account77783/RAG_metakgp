from __future__ import annotations

import re
import importlib.metadata
import json
import tomllib
import unittest
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


from tests.support.paths import REPOSITORY
CONSTRAINTS_PATH = REPOSITORY / "config" / "constraints" / "core-py313.txt"


class DependencyInventoryTests(unittest.TestCase):
    def test_optional_profiles_use_exact_pins_and_reviewed_cpu_artifacts(self) -> None:
        project = tomllib.loads((REPOSITORY / "pyproject.toml").read_text(encoding="utf-8"))
        pins = {}
        for filename in ("core-py313.txt", "linux-py313.txt", "engines-py313.txt"):
            for line in (CONSTRAINTS_PATH.parent / filename).read_text(encoding="utf-8").splitlines():
                if line.strip() and not line.startswith("#"):
                    requirement = Requirement(line)
                    name = canonicalize_name(requirement.name)
                    self.assertNotIn(name, pins)
                    pins[name] = str(requirement.specifier).removeprefix("==")
        for extra in ("tree", "rerank", "visual"):
            for raw in project["project"]["optional-dependencies"][extra]:
                requirement = Requirement(raw)
                self.assertTrue(str(requirement.specifier).startswith("=="))
                self.assertIn(pins[canonicalize_name(requirement.name)], requirement.specifier)
        artifacts = json.loads((REPOSITORY / "config/artifacts/cpu-torch-cp313.json").read_text(encoding="utf-8"))
        self.assertEqual(set(artifacts["profiles"]), {"linux-x86_64", "win-amd64"})
        for profile in artifacts["profiles"].values():
            self.assertEqual({item["name"] for item in profile}, {"torch", "torchvision"})
            for item in profile:
                self.assertEqual(pins[item["name"]], item["version"])
                self.assertTrue(item["version"].endswith("+cpu"))
                self.assertRegex(item["sha256"], r"^[a-f0-9]{64}$")
                self.assertTrue(item["url"].startswith("https://download-r2.pytorch.org/whl/cpu/"))
                self.assertNotIn("cp313t", item["filename"])

    def test_runtime_dependency_closure_is_pinned_including_linux_extra(self) -> None:
        project = tomllib.loads((REPOSITORY / "pyproject.toml").read_text(encoding="utf-8"))
        pins = {}
        for path in (CONSTRAINTS_PATH, CONSTRAINTS_PATH.parent / "linux-py313.txt",
                     CONSTRAINTS_PATH.parent / "engines-py313.txt"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip() and not line.startswith("#"):
                    requirement = Requirement(line)
                    pins[canonicalize_name(requirement.name)] = str(requirement.specifier)
        native_platform = default_environment()["sys_platform"]
        # These pinned platform-only distributions have no dependency children.
        # Cross-platform metadata traversal can omit them on the opposite host;
        # the actual native platform must always install and version-check them.
        foreign_leaves = {"win32": {"pywin32", "colorama"}, "linux": {"uvloop"}}
        for platform in ("win32", "linux"):
            environment = default_environment() | {"sys_platform": platform,
                "platform_system": "Windows" if platform == "win32" else "Linux"}
            pending = [Requirement(line) for line in [*project["project"]["dependencies"],
                       *project["project"]["optional-dependencies"]["test"]]]
            seen = set()
            while pending:
                requirement = pending.pop()
                name = canonicalize_name(requirement.name)
                key = (name, frozenset(requirement.extras))
                if key in seen:
                    continue
                seen.add(key)
                self.assertIn(name, pins, f"Unpinned dependency on {platform}: {requirement}")
                try:
                    distribution = importlib.metadata.distribution(name)
                except importlib.metadata.PackageNotFoundError:
                    self.assertNotEqual(platform, native_platform, f"Missing native dependency: {name}")
                    self.assertIn(name, foreign_leaves[platform], f"Missing foreign dependency metadata: {name}")
                    continue
                if platform == native_platform:
                    self.assertEqual(pins[name], "==" + distribution.version)
                for raw in distribution.requires or []:
                    dependency = Requirement(raw)
                    if dependency.marker is None or any(dependency.marker.evaluate(environment | {"extra": extra})
                            for extra in {"", *requirement.extras}):
                        pending.append(dependency)

    def test_every_declared_exact_dependency_is_captured(self) -> None:
        project = tomllib.loads((REPOSITORY / "pyproject.toml").read_text(encoding="utf-8"))
        declared = [
            *project["build-system"]["requires"],
            *project["project"]["dependencies"],
            *project["project"]["optional-dependencies"]["test"],
        ]
        constraints = {
            line.strip().casefold()
            for path in (CONSTRAINTS_PATH, CONSTRAINTS_PATH.parent / "engines-py313.txt")
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        }
        self.assertTrue(all("==" in requirement for requirement in declared))
        for requirement in declared:
            with self.subTest(requirement=requirement):
                self.assertIn(requirement.casefold(), constraints)

    def test_snapshot_contains_only_unique_exact_pins(self) -> None:
        requirements = [
            line.strip()
            for line in CONSTRAINTS_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        self.assertEqual(len(requirements), 97)
        self.assertTrue(all(re.fullmatch(r"[A-Za-z0-9_.-]+==[^\s]+", item) for item in requirements))
        names = [item.split("==", 1)[0].replace("_", "-").casefold() for item in requirements]
        self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main()
