"""Stable checkout paths regardless of a test module's nesting."""
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
FIXTURES = REPOSITORY / "tests/fixtures"
