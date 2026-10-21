"""Freeze and verify native wheel bundles. Hashes identify bytes, not legal clearance."""
from __future__ import annotations

import argparse
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import platform
import sys
import sysconfig
import zipfile

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.tags import sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename


PROJECT = "graphmind-rag-mcp"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def native_target() -> dict[str, str]:
    return {"implementation": platform.python_implementation(),
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "platform": sysconfig.get_platform()}


def wheel_record(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Wheel inputs must be regular files")
    name, version, _, tags = parse_wheel_filename(path.name)
    if not tags.intersection(sys_tags()):
        raise ValueError(f"Wheel is incompatible with this native target: {path.name}")
    with zipfile.ZipFile(path) as archive:
        # Build tools may vendor other distributions with their own metadata.
        # Only the top-level dist-info describes the installable wheel itself.
        names = [name for name in archive.namelist()
                 if name.endswith(".dist-info/METADATA") and name.count("/") == 1]
        if len(names) != 1:
            raise ValueError("Wheel must contain exactly one package metadata record")
        metadata = BytesParser().parsebytes(archive.read(names[0]))
    if canonicalize_name(metadata["Name"] or "") != name or metadata["Version"] != str(version):
        raise ValueError("Wheel filename and package metadata disagree")
    requires_python = metadata.get("Requires-Python", "")
    if not SpecifierSet(requires_python).contains(platform.python_version(), prereleases=True):
        raise ValueError(f"Unsupported Python version for {name}")
    requirements = metadata.get_all("Requires-Dist", [])
    if any(Requirement(raw).url for raw in requirements):
        raise ValueError("Direct-URL dependencies are excluded from offline bundles")
    return {"filename": path.name, "name": str(name), "version": str(version),
            "sha256": digest(path), "size": path.stat().st_size,
            "requires_python": requires_python, "requires_dist": requirements,
            "extras": sorted(canonicalize_name(value) for value in metadata.get_all("Provides-Extra", [])),
            "license_expression": metadata.get("License-Expression"),
            "license": metadata.get("License"), "tags": sorted(map(str, tags))}


def records_for(bundle: Path) -> list[dict]:
    wheelhouse = bundle / "wheelhouse"
    if wheelhouse.is_symlink() or not wheelhouse.is_dir():
        raise ValueError("Bundle wheelhouse is missing or is a symlink")
    paths = sorted(wheelhouse.iterdir())
    if not paths or any(path.suffix != ".whl" for path in paths):
        raise ValueError("Wheelhouse must contain wheels only")
    records = [wheel_record(path) for path in paths]
    if len({record["name"] for record in records}) != len(records):
        raise ValueError("Wheelhouse must contain one artifact per distribution")
    return sorted(records, key=lambda record: record["name"])


def validate_closure(records: list[dict], extras: list[str]) -> None:
    packages = {record["name"]: record for record in records}
    if PROJECT not in packages:
        raise ValueError("GraphMind wheel is missing")
    if set(extras) - set(packages[PROJECT]["extras"]):
        raise ValueError("Requested GraphMind extra is unavailable")
    pending = [(record["name"], ()) for record in records] + [(PROJECT, tuple(extras))]
    seen = set()
    environment = default_environment()
    while pending:
        name, selected_extras = pending.pop()
        if (name, selected_extras) in seen:
            continue
        seen.add((name, selected_extras))
        for raw in packages[name]["requires_dist"]:
            requirement = Requirement(raw)
            if requirement.marker and not any(
                requirement.marker.evaluate(environment | {"extra": extra})
                for extra in {"", *selected_extras}
            ):
                continue
            dependency = canonicalize_name(requirement.name)
            if dependency not in packages or not requirement.specifier.contains(
                packages[dependency]["version"], prereleases=True
            ):
                raise ValueError(f"Missing or incompatible dependency: {name} requires {requirement.name}")
            if {canonicalize_name(extra) for extra in requirement.extras} - set(packages[dependency]["extras"]):
                raise ValueError(f"Dependency extra is unavailable: {dependency}")
            pending.append((dependency, tuple(sorted(requirement.extras))))


def lock_text(records: list[dict]) -> str:
    return "".join(f"{item['name']}=={item['version']} --hash=sha256:{item['sha256']}\n"
                   for item in records)


def _notice_files(bundle: Path, records: list[dict]) -> dict[str, str]:
    notices = {}
    for record in records:
        with zipfile.ZipFile(bundle / "wheelhouse" / record["filename"]) as archive:
            for name in archive.namelist():
                parts = name.split("/")
                if not name.endswith("/") and ".dist-info/" in name and (
                    "/licenses/" in name or Path(name).name.upper().startswith(("LICENSE", "NOTICE", "COPYING"))
                ):
                    if any(part in {"", ".", ".."} for part in parts) or "\\" in name or ":" in name:
                        raise ValueError("Unsafe notice path in wheel")
                    target = bundle / "notices" / record["name"] / name
                    if not target.resolve().is_relative_to(bundle):
                        raise ValueError("Notice path escapes the bundle")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(name))
                    notices[target.relative_to(bundle).as_posix()] = digest(target)
    return notices


def freeze(bundle: Path, *, extras: list[str] | None = None, provenance: dict | None = None) -> dict:
    bundle = bundle.resolve()
    if (bundle / "manifest.json").exists() or (bundle / "requirements.lock").exists():
        raise ValueError("Preserve the existing bundle; use a fresh directory")
    selected = sorted({canonicalize_name(value) for value in extras or []})
    records = records_for(bundle)
    validate_closure(records, selected)
    notices = _notice_files(bundle, records)
    lock = bundle / "requirements.lock"
    lock.write_bytes(lock_text(records).encode("utf-8"))
    manifest = {"schema_version": 1, "target": native_target(), "extras": selected,
                "wheels": records, "lock_sha256": digest(lock), "notices": notices,
                "provenance": provenance or {}, "rights_disposition": "requires review"}
    # The manifest is the final completion record; partial downloads are never a bundle.
    (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def verify(bundle: Path) -> dict:
    bundle = bundle.resolve()
    manifest_path, lock = bundle / "manifest.json", bundle / "requirements.lock"
    if manifest_path.is_symlink() or lock.is_symlink():
        raise ValueError("Bundle metadata cannot be symlinks")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or manifest.get("target") != native_target():
        raise ValueError("Bundle schema or native target does not match")
    records = records_for(bundle)
    if records != manifest["wheels"]:
        raise ValueError("Wheel inventory or bytes changed")
    validate_closure(records, manifest["extras"])
    if digest(lock) != manifest["lock_sha256"] or lock.read_bytes() != lock_text(records).encode("utf-8"):
        raise ValueError("Dependency lock differs from the wheel inventory")
    for relative, expected in manifest["notices"].items():
        target = bundle / relative
        if not relative.startswith("notices/") or not target.resolve().is_relative_to(bundle) or target.is_symlink():
            raise ValueError("Unsafe notice path")
        if digest(target) != expected:
            raise ValueError("Upstream notice bytes changed")
    return manifest


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("freeze", "verify"))
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--extra", action="append", default=[])
    args = parser.parse_args(argv)
    if args.command == "verify" and args.extra:
        parser.error("verify uses the extras recorded in the bundle")
    try:
        manifest = freeze(args.bundle, extras=args.extra) if args.command == "freeze" else verify(args.bundle)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        parser.exit(2, f"Bundle {args.command} failed ({type(exc).__name__}): {exc}\n")
    print(json.dumps({"target": manifest["target"], "wheels": len(manifest["wheels"]),
                      "extras": manifest["extras"], "rights_disposition": manifest["rights_disposition"],
                      "manifest_sha256": digest(args.bundle / "manifest.json")}, indent=2))


if __name__ == "__main__":
    main()
