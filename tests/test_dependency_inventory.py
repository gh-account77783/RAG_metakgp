from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
CONSTRAINTS_PATH = REPOSITORY / "constraints" / "core-py313.txt"


class DependencyInventoryTests(unittest.TestCase):
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
        self.assertEqual(len(requirements), 84)
        self.assertTrue(all(re.fullmatch(r"[A-Za-z0-9_.-]+==[^\s]+", item) for item in requirements))
        names = [item.split("==", 1)[0].replace("_", "-").casefold() for item in requirements]
        self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main()
