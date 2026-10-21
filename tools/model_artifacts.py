"""Retain public, revision-pinned model assets for explicit offline inference."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import urllib.parse
import urllib.request


MANIFEST = ".graphmind-model.json"
ALLOWED_SUFFIXES = {".json", ".safetensors", ".model", ".txt", ".jinja"}


def validate_source(model: str, revision: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", model):
        raise ValueError("Expected a public namespace/model identifier")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Model revision must be an exact commit SHA")


def allowed_file(name: str) -> bool:
    # This initial profile supports root-level inference assets, never Python or pickle.
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", name):
        return False
    suffix = Path(name).suffix.lower()
    return (suffix in ALLOWED_SUFFIXES
            or (suffix in {"", ".md", ".rst"}
                and name.lower().startswith(("readme", "license", "notice", "copying"))))


def collect(destination: Path, model: str, revision: str, *, max_bytes: int = 12 * 1024**3) -> dict:
    validate_source(model, revision)
    if max_bytes <= 0 or destination.exists():
        raise ValueError("Collection requires a new destination and positive size limit")
    url = f"https://huggingface.co/api/models/{model}/revision/{revision}?blobs=true"
    with urllib.request.urlopen(url, timeout=60) as response:
        metadata = json.loads(response.read(16 * 1024**2 + 1))
    if metadata["sha"] != revision or metadata.get("private") or metadata.get("gated"):
        raise ValueError("Expected the requested public, ungated model revision")
    files = [item for item in metadata["siblings"] if allowed_file(item["rfilename"])]
    names = [item["rfilename"] for item in files]
    if len(set(names)) != len(names) or not any(name.endswith(".safetensors") for name in names):
        raise ValueError("Model must provide unique safetensors inference assets")
    total = sum(item["size"] for item in files)
    if total > max_bytes or any(not isinstance(item["size"], int) or item["size"] < 0 for item in files):
        raise ValueError("Model assets exceed the permitted download size")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(destination.parent).free < total + 2 * 1024**3:
        raise ValueError("Insufficient free disk for model assets and reserve")
    destination.mkdir(exist_ok=False)
    records = []
    for item in files:
        name, size = item["rfilename"], item["size"]
        source = f"https://huggingface.co/{model}/resolve/{revision}/{urllib.parse.quote(name)}"
        sha256 = hashlib.sha256()
        git_blob = hashlib.sha1(f"blob {size}\0".encode(), usedforsecurity=False)
        partial = destination / (name + ".part")
        count = 0
        # No stored token or credential cache is read; only public HTTPS assets.
        with urllib.request.urlopen(source, timeout=120) as response, partial.open("xb") as output:
            while data := response.read(1024**2):
                count += len(data)
                if count > size:
                    raise ValueError("Upstream artifact exceeds its recorded size")
                sha256.update(data)
                git_blob.update(data)
                output.write(data)
        actual = sha256.hexdigest()
        lfs = item.get("lfs")
        matches = actual == lfs["sha256"] if lfs else git_blob.hexdigest() == item["blobId"]
        if count != size or not matches:
            raise ValueError(f"Upstream artifact identity mismatch: {name}")
        partial.rename(destination / name)
        records.append({"filename": name, "size": size, "sha256": actual,
                        "git_blob": item["blobId"], "url": source})
        print(f"Retained {name}: {size} bytes", flush=True)
    manifest = {"schema_version": 1, "model": model, "revision": revision,
                "declared_license": (metadata.get("cardData") or {}).get("license"),
                "rights_disposition": "requires review", "files": records}
    (destination / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def verify(destination: Path) -> dict:
    manifest_path = destination / MANIFEST
    if destination.is_symlink() or manifest_path.is_symlink():
        raise ValueError("Model directory and manifest must not be symlinks")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise ValueError("Unknown model artifact schema")
    validate_source(manifest["model"], manifest["revision"])
    expected = {record["filename"] for record in manifest["files"]}
    if (len(expected) != len(manifest["files"]) or any(not allowed_file(name) for name in expected)
            or not any(name.endswith(".safetensors") for name in expected)):
        raise ValueError("Unsafe model artifact inventory")
    if {path.name for path in destination.iterdir()} != expected | {MANIFEST}:
        raise ValueError("Model artifact inventory changed")
    for item in manifest["files"]:
        path = destination / item["filename"]
        if path.is_symlink() or not path.is_file() or path.stat().st_size != item["size"]:
            raise ValueError("Model artifact missing or replaced")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != item["sha256"]:
                raise ValueError("Model artifact bytes changed")
    return manifest


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("collect", "verify"))
    parser.add_argument("destination", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--revision")
    args = parser.parse_args(argv)
    if args.command == "collect" and not (args.model and args.revision):
        parser.error("collect requires --model and --revision")
    if args.command == "verify" and (args.model or args.revision):
        parser.error("verify uses the recorded model and revision")
    try:
        manifest = (collect(args.destination, args.model, args.revision)
                    if args.command == "collect" else verify(args.destination))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Redirect URLs can contain ephemeral CDN parameters. Do not print them.
        parser.exit(2, f"Model {args.command} failed ({type(exc).__name__}); partial artifacts retained\n")
    print(json.dumps({"model": manifest["model"], "revision": manifest["revision"],
                      "files": len(manifest["files"]), "rights_disposition": manifest["rights_disposition"]}))


if __name__ == "__main__":
    main()
