"""Host-local CLI for document import, reindexing, deletion, and queries."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import signal
import sys
import threading
from dataclasses import asdict, replace
from pathlib import Path

from .config import Settings
from .rag.components.contracts import Job, Outcome, QueryContext
from .errors import ConfigurationError, GraphMindError, JobNotFoundError
from .evaluation import EvaluationRunner, FrozenEvaluationDataset
from .providers import ExtractiveProvider
from .host_admin import complete_submission, folder_sources, import_sources, index_status, selected_engines, wizard
from .service import GraphMindApplication, build_embedding
from .rag.components.vector_store import MemoryVectorStore
from .rag.graphmind.reference_graph import MemoryGraphStore


def _import_options(parser):
    parser.add_argument("--enqueue-only", action="store_true")
    parser.add_argument("--engines", help="Comma-separated configured engine identifiers")
    parser.add_argument("--csv-delimiter", choices=("auto", "comma", "semicolon", "tab", "pipe"))


def _poll_seconds(value):
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Polling must be between 1 and 60 seconds") from exc
    if not math.isfinite(seconds) or not 1 <= seconds <= 60:
        raise argparse.ArgumentTypeError("Polling must be between 1 and 60 seconds")
    return seconds


def _run_worker(executor, poll_seconds):
    stop = threading.Event()
    previous = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, lambda *_: stop.set())
        executor.run_forever(stop, poll_seconds=poll_seconds)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


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

    import_parser = subcommands.add_parser(
        "import", help="Import one PDF, DOCX, TXT, Markdown, or CSV document"
    )
    import_parser.add_argument("path", type=Path)
    _import_options(import_parser)

    folder_parser = subcommands.add_parser("import-folder", help="Import a bounded folder of supported documents")
    folder_parser.add_argument("path", type=Path)
    folder_parser.add_argument("--recursive", action="store_true")
    folder_parser.add_argument("--max-files", type=int, default=100)
    _import_options(folder_parser)
    subcommands.add_parser("wizard", help="Open the host-only configuration/import/status/job menu")

    reindex_parser = subcommands.add_parser(
        "reindex", help="Create and publish a version for the current parser/index configuration"
    )
    reindex_parser.add_argument("path", type=Path)
    _import_options(reindex_parser)

    delete_parser = subcommands.add_parser(
        "delete", help="Hide a document immediately and queue physical cleanup"
    )
    delete_parser.add_argument("document_id")
    delete_parser.add_argument("--enqueue-only", action="store_true")

    retry_parser = subcommands.add_parser("retry", help="Retry a failed durable job")
    retry_parser.add_argument("job_id")

    subcommands.add_parser("worker-once", help="Run at most one queued ingestion job")
    worker = subcommands.add_parser("worker", help="Run the existing durable queue until stopped")
    worker.add_argument("--poll-seconds", type=_poll_seconds, default=5.0)
    subcommands.add_parser("status", help="List documents and durable jobs")
    subcommands.add_parser(
        "embedding-status", help="Compare configured and active index embedding fingerprints"
    )

    serve_parser = subcommands.add_parser(
        "serve", help="Run the authenticated browser and Streamable HTTP MCP service"
    )
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)

    accounts_parser = subcommands.add_parser(
        "accounts", help="Host-only Keycloak account administration"
    )
    accounts_parser.add_argument(
        "action", choices=("list", "disable", "enable", "delete", "restore")
    )
    accounts_parser.add_argument("reference", nargs="?", help="Account UUID or exact email")

    query_parser = subcommands.add_parser("query", help="Query active documents")
    query_parser.add_argument("question")
    query_parser.add_argument("--engine", choices=("graphmind", "tree_navigation", "multimodal"), default="graphmind")
    query_parser.add_argument("--document", action="append", default=[], help="Active source document ID")
    query_parser.add_argument(
        "--extractive",
        action="store_true",
        help="Use the offline acceptance provider instead of the configured model",
    )

    materialize_parser = subcommands.add_parser(
        "eval-materialize", help="Materialize a frozen development evaluation fixture"
    )
    materialize_parser.add_argument("--fixture", type=Path, required=True)
    materialize_parser.add_argument("--source", type=Path, required=True)
    materialize_parser.add_argument("--output", type=Path, required=True)

    evaluate_parser = subcommands.add_parser(
        "evaluate", help="Run and score a frozen evaluation fixture"
    )
    evaluate_parser.add_argument("--fixture", type=Path, required=True)
    evaluate_parser.add_argument("--source", type=Path, required=True)
    evaluate_parser.add_argument("--work-dir", type=Path, required=True)
    evaluate_parser.add_argument("--report", type=Path, required=True)
    evaluate_parser.add_argument("--label", required=True)
    evaluate_parser.add_argument(
        "--extractive", action="store_true", help="Use the offline diagnostic answer provider"
    )
    evaluate_parser.add_argument("--skip-import", action="store_true")
    evaluate_parser.add_argument("--vector-only", action="store_true")
    evaluate_parser.add_argument("--graph-scope", choices=("chunk", "document"))
    evaluate_parser.add_argument("--resolve-model-identity", action="store_true")
    evaluate_parser.add_argument(
        "--memory-stores",
        action="store_true",
        help="Use exact in-memory vector/graph adapters for diagnostic evaluation",
    )
    return parser


def _settings(args: argparse.Namespace) -> Settings:
    explicit: dict[str, object] = {}
    if args.data_dir:
        explicit["data_dir"] = args.data_dir
    if args.allow_root:
        explicit["allowed_import_roots"] = tuple(args.allow_root)
    if getattr(args, "csv_delimiter", None):
        explicit["csv_delimiter"] = args.csv_delimiter
    return Settings.load(
        config_path=args.config,
        dotenv_path=args.dotenv,
        explicit=explicit,
    )


def _job_status(job: Job) -> dict[str, object]:
    return {
        "job_id": job.job_id,
        "operation": job.operation,
        "document_id": job.document_id,
        "version_id": job.version_id,
        "status": job.status.value,
        "attempt_count": job.attempt_count,
        "progress": job.progress,
        "error_code": job.error_code,
    }


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
                "indexes": index_status(app, item.document_id),
            }
            for item in app.metadata.list_documents()
        ],
        "jobs": [_job_status(item) for item in app.metadata.list_jobs()],
    }


def _report_submission(
    app: GraphMindApplication,
    job: Job,
    *,
    enqueue_only: bool,
    warnings: tuple[str, ...] | None = None,
) -> None:
    current, failure = complete_submission(app, job, enqueue_only=enqueue_only)
    output = _status(app)
    output["submitted_job"] = _job_status(current)
    if warnings is not None:
        output["warnings"] = list(warnings)
    print(json.dumps(output, indent=2))
    if failure is not None:
        raise failure


def main(argv: list[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "eval-materialize":
            dataset = FrozenEvaluationDataset(args.fixture, args.source)
            materialized = dataset.materialize(args.output)
            print(
                json.dumps(
                    {
                        "dataset_id": materialized.dataset_id,
                        "document_count": len(materialized.document_paths),
                        "materialization_sha256": materialized.materialization_sha256,
                        "manifest": str(materialized.manifest_path),
                    },
                    indent=2,
                )
            )
            return
        settings = _settings(args)
        if args.command == "config":
            print(json.dumps(settings.diagnostics(), indent=2, ensure_ascii=False))
            return
        if args.command == "accounts":
            from .identity import KeycloakAdminClient

            settings.validate(require_identity_admin_secret=True)
            client = KeycloakAdminClient(settings)
            if args.action == "list":
                output = asyncio.run(client.list_accounts())
            else:
                if not args.reference:
                    parser.error("accounts action requires an account UUID or exact email")
                output = asyncio.run(client.set_state(args.reference, args.action))
            print(json.dumps(output, indent=2, ensure_ascii=False))
            return
        if args.command == "serve":
            if args.port not in range(1, 65_536):
                parser.error("--port must be between 1 and 65535")
            settings.validate(require_identity_secrets=True)
            from .product_server import create_product_server

            try:
                import uvicorn
            except ImportError as exc:
                raise GraphMindError("The server runtime is not installed") from exc
            with GraphMindApplication(settings) as product_application:
                _, asgi = create_product_server(settings, product_application)
                uvicorn.run(
                    asgi,
                    host=args.host,
                    port=args.port,
                    proxy_headers=True,
                    forwarded_allow_ips="127.0.0.1",
                    access_log=False,  # Protocol access logs include OAuth callback query credentials.
                )
            return
        dataset = None
        materialized = None
        if args.command == "evaluate":
            work_dir = args.work_dir.expanduser().resolve()
            dataset = FrozenEvaluationDataset(args.fixture, args.source)
            materialized = dataset.materialize(work_dir / "materialized")
            settings = replace(
                settings,
                data_dir=(settings.data_dir if args.data_dir else work_dir / "runtime"),
                allowed_import_roots=(materialized.root,),
            )
            settings.validate()
        provider = ExtractiveProvider() if getattr(args, "extractive", False) else None
        app_options = {"provider": provider}
        if args.command == "evaluate" and args.memory_stores:
            embedding = build_embedding(settings)
            app_options.update(
                {
                    "embedding": embedding,
                    "vector": MemoryVectorStore(embedding),
                    "graph": MemoryGraphStore(),
                }
            )
            if settings.retrieval_profile == "hybrid":
                app_options["hybrid_vector"] = MemoryVectorStore(embedding)
        with GraphMindApplication(settings, **app_options) as app:
            if args.command == "init":
                print(json.dumps(_status(app), indent=2))
            elif args.command == "doctor":
                statuses = [asdict(item) for item in app.readiness()]
                print(json.dumps(statuses, indent=2))
                if not all(item["available"] for item in statuses):
                    raise SystemExit(2)
            elif args.command in {"import", "reindex"}:
                submission = app.ingestion.prepare_import(args.path, engines=selected_engines(args.engines))
                _report_submission(app, submission.job, enqueue_only=args.enqueue_only,
                                   warnings=submission.warnings)
            elif args.command == "import-folder":
                paths = folder_sources(app, args.path, recursive=args.recursive, max_files=args.max_files)
                output = import_sources(app, paths, engines=selected_engines(args.engines),
                                        enqueue_only=args.enqueue_only, job_status=_job_status)
                print(json.dumps(output, indent=2, ensure_ascii=False))
                if output["failed_files"]:
                    raise SystemExit(2)
            elif args.command == "wizard":
                wizard(app, status=_status, job_status=_job_status)
            elif args.command == "delete":
                submission = app.ingestion.prepare_delete(args.document_id)
                _report_submission(app, submission.job, enqueue_only=args.enqueue_only)
            elif args.command == "retry":
                if app.metadata.job(args.job_id) is None:
                    raise JobNotFoundError("The requested job does not exist")
                app.metadata.retry_job(args.job_id)
                job = app.metadata.job(args.job_id)
                print(
                    json.dumps(
                        {"job_id": args.job_id, "status": job.status.value if job else None},
                        indent=2,
                    )
                )
            elif args.command == "worker-once":
                app.jobs.reconcile()
                job = app.jobs.run_once()
                print(json.dumps({"processed_job": job.job_id if job else None}, indent=2))
            elif args.command == "worker":
                _run_worker(app.jobs, args.poll_seconds)
            elif args.command == "status":
                print(json.dumps(_status(app), indent=2))
            elif args.command == "embedding-status":
                if "graphmind" not in settings.enabled_engines:
                    raise ConfigurationError("GraphMind is not enabled for this installation")
                active = sorted({json.loads(index.recipe.configuration_json)["embedding"]
                                 for index in app.indexes.current("graphmind")})
                current = app.vector.embedding_fingerprint
                print(
                    json.dumps(
                        {
                            "configured_fingerprint": current,
                            "active_fingerprints": active,
                            "compatible": not (set(active) - {current}),
                            "rebuild": (
                                "Reindex every active source with the configured embedding and a new "
                                "Chroma collection before querying."
                            ),
                        },
                        indent=2,
                    )
                )
            elif args.command == "query":
                options = ({"context": QueryContext(args.engine, tuple(args.document))}
                           if args.engine != "graphmind" or args.document else {})
                result = app.answers.query(args.question, **options)
                print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
                if result.outcome is Outcome.ERROR:
                    raise SystemExit(3)
            elif args.command == "evaluate":
                assert dataset is not None and materialized is not None
                if not args.skip_import:
                    for path in materialized.document_paths:
                        submission = app.ingestion.prepare_import(path)
                        app.jobs.run_job(submission.job.job_id)
                if args.vector_only:
                    app.retrieval.graph_limit = 0
                if args.graph_scope:
                    if settings.retrieval_profile == "hybrid" and args.graph_scope != "document":
                        raise ConfigurationError("The hybrid profile collects references from the source document")
                    app.retrieval.graph_scope = args.graph_scope
                embedding_identity = (
                    app.embedding.identity()
                    if hasattr(app.embedding, "identity")
                    else {
                        "provider": "diagnostic-hash",
                        "dimension": app.embedding.dimension,
                        "fingerprint": app.embedding.fingerprint,
                    }
                )
                selected_provider = app.answers.provider
                provider_identity = (
                    selected_provider.identity(resolve=args.resolve_model_identity)
                    if hasattr(selected_provider, "identity")
                    else {"provider": type(selected_provider).__name__}
                )
                runner = EvaluationRunner(
                    dataset,
                    materialized,
                    app.answers,
                    embedding_identity=embedding_identity,
                    provider_identity=provider_identity,
                    retrieval_configuration={
                        "profile": settings.retrieval_profile,
                        "recipe": app.retrieval.identity() if hasattr(app.retrieval, "identity") else {},
                        "seed_limit": app.retrieval.seed_limit,
                        "graph_limit": app.retrieval.graph_limit,
                        "graph_scope": app.retrieval.graph_scope,
                        "max_evidence": app.retrieval.max_evidence,
                        "min_vector_score": app.retrieval.min_vector_score,
                        "max_question_chars": app.retrieval.max_question_chars,
                        "max_concurrent_queries": settings.max_concurrent_queries,
                        "max_queued_queries": settings.max_queued_queries,
                        "query_queue_timeout_seconds": settings.query_queue_timeout_seconds,
                        "vector_only": bool(args.vector_only),
                    },
                    label=args.label,
                )
                report = runner.run(args.report)
                print(json.dumps(report["summary"], indent=2))
                if not report["summary"]["gate_passed"]:
                    raise SystemExit(4)
    except GraphMindError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc)}), file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
