"""Build an allowlisted snapshot and verify its wheel in a clean temporary venv.

Run with CPython 3.13 in a test venv containing the pinned build backends.
Dependencies are installed under the checked-in constraints; PyPI access may be
required. No existing environment or checkout is installed into or cleaned.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILD_FILES = (
    "pyproject.toml", "MANIFEST.in", "README.md", "documentation/AUTH_AND_MCP.md", "documentation/DEPENDENCIES.md", "documentation/ARCHITECTURE.md", "documentation/RELEASE_BUNDLES.md",
    "config/graphmind.example.toml", "config/.env.example", "config/constraints/core-py313.txt", "config/constraints/linux-py313.txt",
    "config/constraints/engines-py313.txt", "config/artifacts/cpu-torch-cp313.json",
    "deploy/keycloak/README.md", "deploy/keycloak/graphmind-realm.template.json",
    "deploy/keycloak/graphmind-user-profile.json", "deploy/acceptance/README.md",
    "tools/run_real_acceptance.py", "tools/mcp_acceptance.py",
    "tools/ubuntu_service_files.py", "tools/ubuntu_backend_files.py", "tools/ubuntu_release_files.py", "deploy/native/README.md",
    "deploy/native/ubuntu.example.json", "deploy/native/ubuntu.backends.example.json",
    "tools/build_keycloak_recovery.py", "deploy/keycloak/graphmind-recovery.json",
    "deploy/keycloak/nginx-recovery.conf.example",
    "deploy/keycloak/themes/graphmind/email/theme.properties",
    "deploy/keycloak/themes/graphmind/email/messages/messages_en.properties",
    "deploy/keycloak/recovery-src/org/graphmind/keycloak/ExistingPasswordResetFactory.java",
    "deploy/keycloak/recovery-src/org/graphmind/keycloak/PasswordRevocationFactory.java",
    "deploy/keycloak/recovery-src/META-INF/services/org.keycloak.authentication.AuthenticatorFactory",
    "deploy/keycloak/recovery-src/META-INF/services/org.keycloak.events.EventListenerProviderFactory",
)

OPERATOR_FILES = BUILD_FILES[3:]
BUILD_FILES += (
    "tools/build_assets.py", "assets/web/graphmind.css", "assets/web/graphmind.js",
    "tools/frontend_assets.py", "assets/web/THIRD_PARTY_NOTICES.txt", "assets/web/graphmind.frontend.json",
    "frontend/package.json", "frontend/package-lock.json", "frontend/tsconfig.json", "frontend/vite.config.mjs",
    "frontend/src/api.ts", "frontend/src/components.tsx", "frontend/src/main.tsx", "frontend/src/types.ts",
    "frontend/src/style.css", "frontend/src/assets.d.ts", "frontend/tests/api.test.mjs",
    "frontend/README.md", "frontend/.gitignore",
)


def run(command, *, cwd: Path, env: dict[str, str]) -> None:
    subprocess.run([str(item) for item in command], cwd=cwd, env=env, check=True)


def inspect_archive(path: Path) -> None:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
        allowed = ("graphmind/", "graphmind_rag_mcp-0.1.0.dist-info/", "graphmind_rag_mcp-0.1.0.data/data/share/graphmind/")
        if not all(name.startswith(allowed) for name in names):
            raise RuntimeError("Wheel contains files outside the explicit package/operator allowlist")
        for required in (
            "graphmind/parsing.py", "graphmind/product_server.py", "graphmind/identity.py",
            "graphmind/rag/graphmind/engine.py", "graphmind/rag/graphmind/reference_graph.py",
            "graphmind/rag/components/contracts.py", "graphmind/rag/components/pipeline.py",
            "graphmind/rag/components/vector_store.py", "graphmind/web/assets.py",
            "graphmind/web/static/graphmind.css", "graphmind/web/static/graphmind.js",
            "graphmind/web/static/THIRD_PARTY_NOTICES.txt", "graphmind/web/static/graphmind.frontend.json",
            "graphmind/rag/tree_navigation/engine.py", "graphmind/rag/multimodal/engine.py",
            "graphmind/rag/components/pdf_pages.py",
        ):
            if required not in names:
                raise RuntimeError(f"Wheel omitted required module: {required}")
        for required in OPERATOR_FILES:
            if required == "MANIFEST.in":
                continue
            suffix = "/share/graphmind/" + required
            if not any(name.endswith(suffix) for name in names):
                raise RuntimeError(f"Wheel omitted operator asset: {required}")
    else:
        with tarfile.open(path) as archive:
            names = [item.name for item in archive.getmembers() if item.isfile()]
        for name in names:
            relative = "/".join(name.split("/")[1:])
            allowed = relative in BUILD_FILES or relative in {"PKG-INFO", "setup.cfg"}
            allowed |= relative.startswith("src/graphmind/") or relative.startswith("src/graphmind_rag_mcp.egg-info/")
            if not allowed:
                raise RuntimeError(f"Source archive contains non-allowlisted file: {relative}")
    forbidden = ("__pycache__", ".pyc", ".pyo", "/.env", "/docs/", "/legacy/", "/Crawler/", "/VectorStore/")
    for name in names:
        # The secret-free .env.example is a required template, not a runtime dotenv.
        if name.endswith("/.env.example"):
            continue
        if any(part in name for part in forbidden) or ".." in Path(name).parts:
            raise RuntimeError("Archive contains excluded runtime/private content")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional directory to retain verified artifacts/report")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 13):
        parser.error("Current supported package/test baseline is CPython 3.13")
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("GRAPHMIND_", "NEO4J_", "OLLAMA_")) or key in {"PYTHONPATH", "PYTHONHOME"}:
            env.pop(key)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    with tempfile.TemporaryDirectory(prefix="graphmind-package-") as temporary:
        root = Path(temporary)
        snapshot = root / "source"
        for relative in BUILD_FILES:
            target = snapshot / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)
        shutil.copytree(ROOT / "src" / "graphmind", snapshot / "src" / "graphmind",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
        dist = root / "dist"
        run([sys.executable, "-m", "build", "--no-isolation", "--config-setting=--build-option=--bdist-dir=b",
             "--outdir", dist, snapshot], cwd=root, env=env)
        artifacts = sorted(dist.iterdir())
        if len(artifacts) != 2:
            raise RuntimeError("Expected exactly one wheel and one source archive")
        for artifact in artifacts:
            inspect_archive(artifact)
        wheel = next(path for path in artifacts if path.suffix == ".whl")
        target_venv = root / "installed"
        venv.EnvBuilder(with_pip=True).create(target_venv)
        python = target_venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        run([python, "-m", "pip", "install", "--constraint", snapshot / "config/constraints/core-py313.txt",
             "--constraint", snapshot / "config/constraints/linux-py313.txt",
             "--constraint", snapshot / "config/constraints/engines-py313.txt",
             f"{wheel}[test]"], cwd=root, env=env)
        run([python, "-m", "pip", "check"], cwd=root, env=env)
        run([python, "-c", "import graphmind, pathlib, sys; p=pathlib.Path(graphmind.__file__).resolve(); "
             "assert p.is_relative_to(pathlib.Path(sys.prefix).resolve()), p; print('Installed import:', p)"], cwd=root, env=env)
        run([python, "-c", "from importlib.resources import files; "
             "from graphmind.web.assets import ASSET_NAMES, read_asset; "
             "assert all(files('graphmind.web').joinpath('static', n).is_file() and read_asset(n) "
             "for n in ASSET_NAMES); print('Installed browser resources: passed')"], cwd=root, env=env)
        run([python, "-m", "graphmind.cli", "--help"], cwd=root, env=env)
        run([python, "-m", "graphmind.cli", "serve", "--help"], cwd=root, env=env)
        run([python, "-m", "graphmind.cli", "accounts", "--help"], cwd=root, env=env)
        # Tests need development fixture/legacy namespaces, not checkout src.
        # graphmind exists only under src/, so this cannot shadow the installed wheel.
        env["PYTHONPATH"] = str(ROOT)
        run([python, "-c", "import graphmind, pathlib, sys; "
             "assert pathlib.Path(graphmind.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve())"],
            cwd=root, env=env)
        run([python, "-B", "-m", "unittest", "discover", "-s", ROOT / "tests",
             "-t", ROOT, "-v"], cwd=root, env=env)
        report = {
            "python": sys.version.split()[0], "platform": sys.platform,
            "installed_wheel_suite": "passed", "pip_check": "passed", "archive_allowlist": "passed",
            "external_services": "not tested",
            "sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in artifacts},
        }
        if args.output:
            args.output.mkdir(parents=True, exist_ok=True)
            for artifact in artifacts:
                shutil.copy2(artifact, args.output / artifact.name)
            (args.output / "package-verification.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
