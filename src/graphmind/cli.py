"""Host-local CLI for document import, reindexing, deletion, and queries."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path

from .config import Settings
from .domain import Outcome
from .errors import GraphMindError
from .evaluation import EvaluationRunner, FrozenEvaluationDataset
from .providers import ExtractiveProvider
from .service import GraphMindApplication, build_embedding
from .storage import MemoryGraphStore, MemoryVectorStore


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
    import_parser.add_argument("--enqueue-only", action="store_true")
    import_parser.add_argument(
        "--csv-delimiter", choices=("auto", "comma", "semicolon", "tab", "pipe")
    )

    reindex_parser = subcommands.add_parser(
        "reindex", help="Create and publish a version for the current parser/index configuration"
    )
    reindex_parser.add_argument("path", type=Path)
    reindex_parser.add_argument("--enqueue-only", action="store_true")
    reindex_parser.add_argument(
        "--csv-delimiter", choices=("auto", "comma", "semicolon", "tab", "pipe")
    )

    delete_parser = subcommands.add_parser(
        "delete", help="Hide a document immediately and queue physical cleanup"
    )
    delete_parser.add_argument("document_id")
    delete_parser.add_argument("--enqueue-only", action="store_true")

    retry_parser = subcommands.add_parser("retry", help="Retry a failed durable job")
    retry_parser.add_argument("job_id")

    subcommands.add_parser("worker-once", help="Run at most one queued ingestion job")
    subcommands.add_parser("status", help="List documents and durable jobs")
    subcommands.add_parser(
        "embedding-status", help="Compare configured and active index embedding fingerprints"
    )

    query_parser = subcommands.add_parser("query", help="Query active documents")
    query_parser.add_argument("question")
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
                "progress": item.progress,
                "error_code": item.error_code,
            }
            for item in app.metadata.list_jobs()
        ],
    }


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
        with GraphMindApplication(settings, **app_options) as app:
            if args.command == "init":
                print(json.dumps(_status(app), indent=2))
            elif args.command == "doctor":
                statuses = [asdict(item) for item in app.readiness()]
                print(json.dumps(statuses, indent=2))
                if not all(item["available"] for item in statuses):
                    raise SystemExit(2)
            elif args.command in {"import", "reindex"}:
                submission = app.ingestion.prepare_import(args.path)
                if not args.enqueue_only and submission.job.status.value != "succeeded":
                    if submission.job.status.value == "failed":
                        app.metadata.retry_job(submission.job.job_id)
                    app.jobs.run_once()
                output = _status(app)
                output["warnings"] = list(submission.warnings)
                print(json.dumps(output, indent=2))
            elif args.command == "delete":
                submission = app.ingestion.prepare_delete(args.document_id)
                if not args.enqueue_only and submission.job.status.value != "succeeded":
                    if submission.job.status.value == "failed":
                        app.metadata.retry_job(submission.job.job_id)
                    app.jobs.run_once()
                print(json.dumps(_status(app), indent=2))
            elif args.command == "retry":
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
            elif args.command == "status":
                print(json.dumps(_status(app), indent=2))
            elif args.command == "embedding-status":
                active = sorted(app.metadata.active_embedding_fingerprints())
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
                result = app.answers.query(args.question)
                print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
                if result.outcome is Outcome.ERROR:
                    raise SystemExit(3)
            elif args.command == "evaluate":
                assert dataset is not None and materialized is not None
                if not args.skip_import:
                    for path in materialized.document_paths:
                        submission = app.ingestion.prepare_import(path)
                        if submission.job.status.value == "failed":
                            app.metadata.retry_job(submission.job.job_id)
                        if submission.job.status.value != "succeeded":
                            app.jobs.run_once()
                        completed = app.metadata.job(submission.job.job_id)
                        if completed is None or completed.status.value != "succeeded":
                            raise GraphMindError(f"Evaluation import failed for {path.name}")
                if args.vector_only:
                    app.retrieval.graph_limit = 0
                if args.graph_scope:
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
