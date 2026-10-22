"""Load fixed public assets from the package or its explicit source checkout."""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path


ASSET_NAMES = ("graphmind.css", "graphmind.js", "THIRD_PARTY_NOTICES.txt")


def read_asset(name: str) -> str:
    """Never resolve request paths, cwd files or remote URLs."""
    if name not in ASSET_NAMES:
        raise ValueError("Unknown public asset")
    resource = files("graphmind.web").joinpath("static", name)
    if resource.is_file():
        return resource.read_text(encoding="utf-8")
    module_path = Path(__file__).resolve()
    source_root = module_path.parents[3]
    if (source_root / "src/graphmind/web/assets.py").resolve() == module_path:
        return (source_root / "assets/web" / name).read_text(encoding="utf-8")
    raise FileNotFoundError(f"Packaged public asset is missing: {name}")


CSS = read_asset("graphmind.css")
JAVASCRIPT = read_asset("graphmind.js")
NOTICES = read_asset("THIRD_PARTY_NOTICES.txt")
