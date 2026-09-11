"""Host-local CLI for the P1/P2 application slice."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .config import Settings
from .domain import Outcome
from .errors import GraphMindError
from .providers import ExtractiveProvider
from .service import GraphMindApplication


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="graphmind", description="GraphMind host administration")
    parser.add_argument("--config", type=Path, help="TOML configuration file")
    parser.add_argument("--dotenv", type=Path, help="Environment file")
    parser.add_argument("--data-dir", type=Path, help="Private runtime data directory")
    parser.add_argument(
        "--allow-root",
        action="append",
        type=Path,
        help="Allowed import root; may be repeated",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("init", help="Create private paths and initialize metadata")
    subcommands.add_parser("config", help="Print redacted effective configuration")
    subcommands.add_parser("doctor", help="Check real storage dependencies")

    import_parser = subcommands.add_parser("import", help="Import one UTF-8 TXT document")
    import_parser.add_argument("path", type=Path)
    import_parser.add_argument("--enqueue-only", action="store_true")

    subcommands.add_parser("worker-once", help="Run at most one queued ingestion job")
    subcommands.add_parser("status", help="List documents and durable jobs")

    query_parser = subcommands.add_parser("query", help="Query active documents")
    query_parser.add_argument("question")
    query_parser.add_argument(
        "--extractive",
        action="store_true",
        help="Use the offline acceptance provider instead of the configured model",
    )
    return parser


def _settings(args: argparse.Namespace) -> Settings:
    explicit: dict[str, object] = {}
    if args.data_dir:
        explicit["data_dir"] = args.data_dir
    if args.allow_root:
        explicit["allowed_import_roots"] = tuple(args.allow_root)
    return Settings.load(
        config_path=args.config,
        dotenv_path=args.dotenv,
        explicit=explicit,
    )


def _status(app: GraphMindApplication) -> dict[str, object]:
    return {
        "schema_version": app.metadata.schema_version(),
        "installation_id": app.installation_id,
        "documents": [
            {
                "document_id": item.document_id,
                "display_name": item.display_name,
                "active_version_id": item.active_version_id,
                "deleted": item.deleted_at is not None,
            }
            for item in app.metadata.list_documents()
        ],
        "jobs": [
            {
                "job_id": item.job_id,
                "operation": item.operation,
                "version_id": item.version_id,
                "status": item.status.value,
                "attempt_count": item.attempt_count,
                "error_code": item.error_code,
            }
            for item in app.metadata.list_jobs()
        ],
    }


def main(argv: list[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        settings = _settings(args)
        if args.command == "config":
            print(json.dumps(settings.diagnostics(), indent=2, ensure_ascii=False))
            return
        provider = ExtractiveProvider() if getattr(args, "extractive", False) else None
        with GraphMindApplication(settings, provider=provider) as app:
            if args.command == "init":
                print(json.dumps(_status(app), indent=2))
            elif args.command == "doctor":
                statuses = [asdict(item) for item in app.readiness()]
                print(json.dumps(statuses, indent=2))
                if not all(item["available"] for item in statuses):
                    raise SystemExit(2)
            elif args.command == "import":
                submission = app.ingestion.prepare_txt_import(args.path)
                if not args.enqueue_only and submission.job.status.value != "succeeded":
                    if submission.job.status.value == "failed":
                        app.metadata.retry_job(submission.job.job_id)
                    app.jobs.run_once()
                print(json.dumps(_status(app), indent=2))
            elif args.command == "worker-once":
                app.jobs.reconcile()
                job = app.jobs.run_once()
                print(json.dumps({"processed_job": job.job_id if job else None}, indent=2))
            elif args.command == "status":
                print(json.dumps(_status(app), indent=2))
            elif args.command == "query":
                result = app.answers.query(args.question)
                print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
                if result.outcome is Outcome.ERROR:
                    raise SystemExit(3)
    except GraphMindError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc)}), file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
