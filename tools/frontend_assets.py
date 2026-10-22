"""Record and verify the static reader build without running npm during packaging."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess


SOURCE_FILES = (
    "frontend/package.json", "frontend/package-lock.json", "frontend/tsconfig.json", "frontend/vite.config.mjs",
    "frontend/src/api.ts", "frontend/src/components.tsx", "frontend/src/main.tsx", "frontend/src/types.ts",
    "frontend/src/style.css",
    "frontend/src/assets.d.ts",
)
ASSETS = ("graphmind.js", "graphmind.css", "THIRD_PARTY_NOTICES.txt")


def digest(path: Path) -> str:
    # Source archives/checkouts can use native newlines; the source identity is
    # independent of LF/CRLF. The generated release assets themselves stay LF.
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def verify(root: Path) -> dict:
    path = root / "assets/web/graphmind.frontend.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if (manifest["schema_version"] != 1 or set(manifest["sources"]) != set(SOURCE_FILES)
            or set(manifest["assets"]) != set(ASSETS)):
        raise ValueError("Frontend build inventory differs")
    for name, expected in manifest["sources"].items():
        if digest(root / name) != expected:
            raise ValueError(f"Frontend source changed; rebuild the static reader: {name}")
    for name, expected in manifest["assets"].items():
        if digest(root / "assets/web" / name) != expected:
            raise ValueError(f"Frontend output changed: {name}")
    return manifest


def record(root: Path, node: str) -> dict:
    modules = root / "frontend/node_modules"
    notices = ["GraphMind static reader — third-party software notices\n",
               "This inventory includes browser and build-tool dependencies. "
               "Not every build dependency is present in the browser bundle.\n"]
    packages = []
    for path in sorted(modules.rglob("package.json")):
        # Package roots only, excluding example/project metadata inside packages.
        relative = path.parent.relative_to(modules)
        if len(relative.parts) > 2 and "node_modules" not in relative.parts[1:]:
            continue
        metadata = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(metadata.get("name"), str) or not isinstance(metadata.get("version"), str):
            continue
        packages.append({key: metadata.get(key, "UNSPECIFIED") for key in ("name", "version", "license")})
        notices.append(f"\n{'=' * 72}\n{metadata['name']} {metadata['version']}\nDeclared license: {metadata.get('license', 'UNSPECIFIED')}\n")
        licenses = [item for item in path.parent.iterdir() if item.is_file()
                    and item.name.casefold().startswith(("license", "licence", "copying", "notice"))]
        for license_path in sorted(licenses):
            notices.append(f"\n{license_path.name}\n{license_path.read_text(encoding='utf-8', errors='replace')}\n")
        if not licenses:
            notices.append("No separate root license/notice text was present in this npm artifact.\n")
    if not {"react", "react-dom", "scheduler"} <= {item["name"] for item in packages}:
        raise ValueError("Installed frontend package inventory is incomplete")
    assets = root / "assets/web"
    (assets / "THIRD_PARTY_NOTICES.txt").write_text("\n".join(notices), encoding="utf-8", newline="\n")
    lock = json.loads((root / "frontend/package-lock.json").read_text(encoding="utf-8"))
    for name, package in lock["packages"].items():
        if name and (not package.get("integrity", "").startswith("sha512-")
                     or not package.get("resolved", "").startswith("https://registry.npmjs.org/")):
            raise ValueError("The frontend lock must retain exact public package hashes")
    manifest = {"schema_version": 1, "node": subprocess.check_output([node, "--version"], text=True).strip(),
                "format": "static-iife", "sources": {name: digest(root / name) for name in SOURCE_FILES},
                "assets": {name: digest(assets / name) for name in ASSETS}, "packages": packages}
    (assets / "graphmind.frontend.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    return verify(root)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("record", "verify"))
    parser.add_argument("--node", default="node")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    result = record(root, args.node) if args.action == "record" else verify(root)
    print(json.dumps({"node": result["node"], "source_files": len(result["sources"]),
                      "packages": len(result["packages"]), "verified": True}))
