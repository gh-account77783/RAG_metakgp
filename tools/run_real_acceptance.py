"""Explicit opt-in real-service checks; no skipped suite is reported as passed."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def stores(settings) -> None:
    # Preserve real credentials privately while forcing disposable diagnostic stores,
    # hash embeddings and no original-data/model assumptions.
    with tempfile.TemporaryDirectory(prefix="graphmind-store-acceptance-") as temporary:
        dotenv = Path(temporary) / "test.env"
        fields = {"NEO4J_URI": settings.neo4j_uri, "NEO4J_USERNAME": settings.neo4j_username,
                  "NEO4J_PASSWORD": settings.neo4j_password, "NEO4J_DATABASE": settings.neo4j_database}
        if any("\n" in value or "\r" in value for value in fields.values()):
            raise ValueError("Test connection settings must be single-line values")
        dotenv.write_text("\n".join(f'{key}="{value}"' for key, value in fields.items()) + "\n", encoding="utf-8")
        dotenv.chmod(0o600)
        from graphmind.storage import Neo4jGraphStore
        graph = Neo4jGraphStore(settings.neo4j_uri, settings.neo4j_username, settings.neo4j_password, settings.neo4j_database)
        try:
            deadline = time.monotonic() + 60
            while not graph.readiness().available:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Real Neo4j readiness failed; integration did not run")
                time.sleep(1)
        finally:
            graph.close()
        os.environ["GRAPHMIND_RUN_INTEGRATION"] = "1"
        os.environ["GRAPHMIND_TEST_DOTENV"] = str(dotenv)
        # These optional provider cases belong to separate explicit real-model runs.
        os.environ.pop("GRAPHMIND_RUN_MODEL_INTEGRATION", None)
        os.environ.pop("GRAPHMIND_RUN_LOCAL_MODEL_INTEGRATION", None)
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests" / "integration"), pattern="test_*.py")
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        optional_skips = [test.id() for test, _ in result.skipped]
        mandatory = [test for test in optional_skips if "ModelIntegration" not in test and "LocalModel" not in test]
        if mandatory or not result.wasSuccessful() or result.testsRun - len(result.skipped) == 0:
            raise RuntimeError("Real-store acceptance failed or did not execute")
        print("Real-store checks passed; hosted/local-model and identity/client acceptance are NOT included.")


async def identity(settings) -> None:
    from graphmind.identity import KeycloakAuthority
    authority = KeycloakAuthority(settings)
    await authority.readiness()
    token = os.environ.get("GRAPHMIND_TEST_ACCESS_TOKEN", "")
    revoked = os.environ.get("GRAPHMIND_TEST_REVOKED_TOKEN", "")
    if not token or not revoked:
        raise RuntimeError("Set private verified and revoked test credentials; identity acceptance did not run")
    if await authority.verify_token(token) is None:
        raise RuntimeError("Verified test reader was rejected")
    if await authority.verify_token(revoked) is not None:
        raise RuntimeError("Revoked test reader was accepted")
    print("Fresh discovery and verified/revoked introspection passed; signup/mail/Google/browser/client gates remain manual.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stores", action="store_true")
    parser.add_argument("--identity", action="store_true")
    parser.add_argument("--dotenv", type=Path, help="Explicit private test environment, never an implicit repository .env")
    args = parser.parse_args()
    if not (args.stores or args.identity):
        parser.error("Select --stores and/or --identity explicitly")
    from graphmind.config import Settings
    settings = Settings.load(dotenv_path=args.dotenv or Path(tempfile.gettempdir()) / "graphmind-no-implicit-dotenv",
                             environ=os.environ)
    if args.stores:
        settings.validate(require_neo4j_secret=True)
        stores(settings)
    if args.identity:
        asyncio.run(identity(settings))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Never print endpoint credentials, tokens, response bodies or full exceptions.
        print(f"Acceptance incomplete ({type(exc).__name__}); inspect private configuration and the recorded test result.", file=sys.stderr)
        raise SystemExit(1) from None
