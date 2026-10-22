"""Host-only operator helpers over the existing source and durable-job authority."""
from dataclasses import asdict
import json
import os
from pathlib import Path

from .errors import GraphMindError, JobNotFoundError, PathValidationError
from .parsing import SUPPORTED_MEDIA_TYPES


def selected_engines(value):
    return None if value is None else tuple(item.strip() for item in value.split(',') if item.strip())


def index_status(app, document_id):
    return [{"index_id": index.index_id, "engine_id": index.recipe.engine_id,
             "source_version_id": index.source_version_id, "status": index.status.value,
             "failure_code": index.failure_code,
             "active": bool((head := app.indexes.ready(document_id, index.recipe.engine_id))
                            and head.index_id == index.index_id)}
            for index in app.indexes.for_document(document_id)]


def complete_submission(app, job, *, enqueue_only=False):
    failure = None
    if not enqueue_only:
        try:
            app.jobs.run_job(job.job_id)
        except GraphMindError as exc:
            failure = exc
    current = app.metadata.job(job.job_id)
    if current is None:
        raise JobNotFoundError("The submitted job does not exist")
    return current, failure


def folder_sources(app, folder: Path, *, recursive=False, max_files=100):
    if type(max_files) is not int or not 1 <= max_files <= 1000:
        raise PathValidationError("max_files must be between 1 and 1000")
    root = app.ingestion.resolve_import_path(folder, directory=True)
    runtime = app.settings.data_dir.resolve()
    if root.is_relative_to(runtime):
        raise PathValidationError("The application data directory cannot be an import folder")
    selected = []
    def unreadable(error):
        raise PathValidationError("An import folder cannot be read") from error
    for current, directories, files in os.walk(root, followlinks=False, onerror=unreadable):
        allowed = []
        for name in sorted(directories) if recursive else ():
            child = Path(current) / name
            if (not name.startswith('.') and not child.is_symlink() and not child.is_junction()
                    and not child.resolve().is_relative_to(runtime)):
                allowed.append(name)
        directories[:] = allowed
        for name in sorted(files):
            path = Path(current) / name
            if path.suffix.casefold() not in SUPPORTED_MEDIA_TYPES or path.is_symlink() or path.is_junction():
                continue
            selected.append(app.ingestion.resolve_import_path(path))
            if len(selected) > max_files:
                raise PathValidationError("Folder selection exceeds max_files; select a smaller folder or raise the limit")
    if not selected:
        raise PathValidationError("The folder contains no supported documents")
    return selected


def import_sources(app, paths, *, engines=None, enqueue_only=False, job_status):
    results = []
    for path in paths:
        record = {"path": str(path)}
        try:
            submission = app.ingestion.prepare_import(path, engines=engines)
            current, failure = complete_submission(app, submission.job, enqueue_only=enqueue_only)
            record.update(submitted_job=job_status(current), warnings=list(submission.warnings),
                          indexes=index_status(app, submission.document.document_id))
            if failure is not None:
                record["error"] = failure.code
        except GraphMindError as exc:
            record.update(error=exc.code, message=str(exc))
        results.append(record)
    return {"files": results, "failed_files": sum("error" in item for item in results)}


def wizard(app, *, status, job_status):
    """Interactive host menu; no web/MCP mutation or second configuration store."""
    print("GraphMind host administration\nData: " + str(app.settings.data_dir))
    while True:
        print("1 Configuration  2 Check dependencies  3 Import file/folder  4 Status  5 Retry job  6 Delete document  0 Exit")
        try:
            action = input("Action: ").strip()
            if action == "0":
                return
            if action == "1":
                output = app.settings.diagnostics()
            elif action == "2":
                output = [asdict(item) for item in app.readiness()]
            elif action == "3":
                path = Path(input("File or folder: ").strip())
                choice = input("Engines (comma separated; blank uses configured compatible engines): ").strip()
                engines = selected_engines(choice or None)
                paths = folder_sources(app, path, recursive=input("Include subfolders? [y/N]: ").casefold() == 'y') if path.is_dir() else [path]
                output = import_sources(app, paths, engines=engines, job_status=job_status)
            elif action == "4":
                output = status(app)
            elif action == "5":
                identifier = input("Failed job ID: ").strip()
                app.metadata.retry_job(identifier)
                job = app.metadata.job(identifier)
                if job is None:
                    raise JobNotFoundError("The requested job does not exist")
                current, failure = complete_submission(app, job)
                output = {"submitted_job": job_status(current), **({"error": failure.code} if failure else {})}
            elif action == "6":
                submission = app.ingestion.prepare_delete(input("Document ID: ").strip())
                current, failure = complete_submission(app, submission.job)
                output = {"submitted_job": job_status(current), **({"error": failure.code} if failure else {})}
            else:
                print("Choose a listed action."); continue
            print(json.dumps(output, indent=2, ensure_ascii=False))
        except EOFError:
            return
        except GraphMindError as exc:
            print(json.dumps({"error": exc.code, "message": str(exc)}))
