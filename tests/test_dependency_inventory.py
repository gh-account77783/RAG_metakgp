from __future__ import annotations

import re
import importlib.metadata
import tomllib
import unittest
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


REPOSITORY = Path(__file__).resolve().parents[1]
CONSTRAINTS_PATH = REPOSITORY / "constraints" / "core-py313.txt"


class DependencyInventoryTests(unittest.TestCase):
    def test_runtime_dependency_closure_is_pinned_including_linux_extra(self) -> None:
        project = tomllib.loads((REPOSITORY / "pyproject.toml").read_text(encoding="utf-8"))
        pins = {}
        for path in (CONSTRAINTS_PATH, REPOSITORY / "constraints" / "linux-py313.txt"):
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
            for line in CONSTRAINTS_PATH.read_text(encoding="utf-8").splitlines()
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
