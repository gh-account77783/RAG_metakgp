from __future__ import annotations

import io
import os
import sys
import tempfile
import time
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from graphmind.config import Settings
from graphmind.embeddings import HashEmbedding
from graphmind.errors import (
    ConfigurationError,
    DocumentBusyError,
    DocumentEncodingError,
    DocumentParseError,
    EmptyDocumentError,
    EncryptedDocumentError,
    JobQueueFullError,
    ManifestUnavailableError,
    OcrRequiredError,
    ParserLimitError,
    ParserTimeoutError,
    PublicationError,
)
from graphmind.parsing import IsolatedExtractor
from graphmind.providers import ExtractiveProvider
from graphmind.service import GraphMindApplication
from graphmind.storage import MemoryGraphStore, MemoryVectorStore


def make_settings(root: Path, **overrides) -> Settings:
    values = {
        "data_dir": root / "runtime with spaces Ω",
        "allowed_import_roots": (root,),
        "chunk_size": 96,
        "chunk_overlap": 12,
        "embedding_dimension": 64,
        "job_lease_seconds": 10,
        "parser_timeout_seconds": 5,
    }
    values.update(overrides)
    return Settings.load(dotenv_path=root / "missing.env", environ={}, explicit=values)


def make_app(root: Path, *, settings: Settings | None = None, vector=None, graph=None, failure=None):
    selected = settings or make_settings(root)
    selected_vector = vector or MemoryVectorStore(HashEmbedding(selected.embedding_dimension))
    selected_graph = graph or MemoryGraphStore()
    return GraphMindApplication(
        selected,
        vector=selected_vector,
        graph=selected_graph,
        provider=ExtractiveProvider(),
        failure_injector=failure,
    )


def run_submission(app: GraphMindApplication, path: Path):
    submission = app.ingestion.prepare_import(path)
    if submission.job.status.value == "failed":
        app.metadata.retry_job(submission.job.job_id)
    if submission.job.status.value != "succeeded":
        app.jobs.run_once()
    return submission


def docx_bytes(*, external: bool = False, empty: bool = False) -> bytes:
    paragraph = "" if empty else "<w:p><w:r><w:t>Résumé Ω paragraph</w:t></w:r></w:p>"
    table = "" if empty else (
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Alpha</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>Beta</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{paragraph}{table}<w:sectPr/></w:body></w:document>"
    )
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
        archive.writestr("[Content_Types].xml", "<Types/>")
        if external:
            archive.writestr(
                "word/_rels/document.xml.rels",
                '<Relationships><Relationship TargetMode="External" Target="https://invalid.example/"/></Relationships>',
            )
            archive.writestr("word/vbaProject.bin", b"not executable")
    return target.getvalue()


def pdf_bytes(pages: list[str], *, encrypted: bool = False) -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    for value in pages:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
        )
        if value:
            safe = value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET".encode("latin-1"))
            page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("secret")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class FormatMatrixTests(unittest.TestCase):
    def test_markdown_structure_references_and_untrusted_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "guide.md"
            source.write_text(
                "# Setup\nRead [[details.txt]]. <script>ignored()</script>\n\n"
                "| Key | Value |\n| --- | --- |\n| mode | safe |\n\n"
                "```python\nprint('text only')\n```\nhttps://invalid.example/no-fetch",
                encoding="utf-8",
            )
            with make_app(root) as app:
                submission = run_submission(app, source)
                chunks = app.metadata.all_chunks_for_version(submission.version.version_id)
                locators = " ".join(chunk.locator for chunk in chunks)
                self.assertIn("heading", locators)
                self.assertIn("table", locators)
                self.assertIn("code block", locators)
                self.assertIn("details.txt", {ref for chunk in chunks for ref in chunk.reference_names})
                self.assertIn("raw_html_is_untrusted_text", submission.warnings)
                self.assertIn("external_links_are_not_fetched", submission.warnings)

    def test_csv_quotes_newlines_unicode_delimiter_headers_and_formulas(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "records.csv"
            source.write_text(
                'name;note;amount\n"Zoë";"line one\nline two";"=2+2"\n"李";"ok";"7"\n',
                encoding="utf-8",
            )
            with make_app(root) as app:
                submission = run_submission(app, source)
                chunks = app.metadata.all_chunks_for_version(submission.version.version_id)
                self.assertTrue(all(chunk.locator.startswith("row ") for chunk in chunks))
                text = " ".join(chunk.text for chunk in chunks)
                self.assertIn("Zoë", text)
                self.assertIn("line one\nline two", text)
                self.assertIn("=2+2", text)
                self.assertIn("spreadsheet_formulas_are_inert_text", submission.warnings)

            declared = root / "declared.csv"
            declared.write_text("name|note\nalpha|contains,many,commas\n", encoding="utf-8")
            settings = make_settings(root, csv_delimiter="pipe")
            with make_app(root, settings=settings) as app:
                submission = run_submission(app, declared)
                self.assertIn("delimiter=pipe", submission.version.extractor_fingerprint)
                self.assertIn(
                    "contains,many,commas",
                    app.metadata.all_chunks_for_version(submission.version.version_id)[0].text,
                )
            with self.assertRaises(ConfigurationError):
                make_settings(root, csv_delimiter="colon")

    def test_docx_paragraph_table_and_inert_embedded_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "report.docx"
            payload = docx_bytes(external=True)
            source.write_bytes(payload)
            with make_app(root) as app:
                submission = run_submission(app, source)
                chunks = app.metadata.all_chunks_for_version(submission.version.version_id)
                locators = " ".join(chunk.locator for chunk in chunks)
                self.assertIn("paragraph 1", locators)
                self.assertIn("table 1 row 1 columns 1-2", locators)
                self.assertIn("embedded_objects_and_external_relationships_are_not_processed", submission.warnings)
                self.assertEqual(
                    (app.settings.data_dir / submission.version.private_file_ref).read_bytes(), payload
                )

    def test_pdf_multipage_text_and_page_locators(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "manual.pdf"
            source.write_bytes(pdf_bytes(["Page one alpha", "Page two beta"]))
            with make_app(root) as app:
                submission = run_submission(app, source)
                chunks = app.metadata.all_chunks_for_version(submission.version.version_id)
                self.assertEqual([chunk.locator for chunk in chunks], ["page 1", "page 2"])
                self.assertIn("Page one alpha", chunks[0].text)
                self.assertIn("pdf_layout_and_reading_order_may_not_be_preserved", submission.warnings)

    def test_format_failures_are_typed_and_do_not_poison_next_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases: list[tuple[str, bytes, type[Exception]]] = [
                ("bad.md", b"\xff", DocumentEncodingError),
                ("empty.markdown", b"  \n", EmptyDocumentError),
                ("bad.csv", b"1,2\n3,4\n", DocumentParseError),
                ("missing-header.csv", b"name,,value\na,b,c\n", DocumentParseError),
                ("malformed.csv", b'name,note\nalpha,"unterminated\n', DocumentParseError),
                ("bad.docx", b"not-a-zip", DocumentParseError),
                ("empty.docx", docx_bytes(empty=True), EmptyDocumentError),
                ("corrupt.pdf", b"%PDF-corrupt", DocumentParseError),
                ("zero-pages.pdf", pdf_bytes([]), EmptyDocumentError),
                ("scan.pdf", pdf_bytes([""]), OcrRequiredError),
                ("locked.pdf", pdf_bytes(["secret"], encrypted=True), EncryptedDocumentError),
            ]
            with make_app(root) as app:
                for name, payload, error in cases:
                    with self.subTest(name=name):
                        path = root / name
                        path.write_bytes(payload)
                        with self.assertRaises(error):
                            app.ingestion.prepare_import(path)
                self.assertEqual(app.metadata.list_documents(), [])
                valid = root / "valid.txt"
                valid.write_text("A valid import still works after parser failures.", encoding="utf-8")
                submission = run_submission(app, valid)
                self.assertEqual(
                    app.metadata.document(submission.document.document_id).active_version_id,
                    submission.version.version_id,
                )

    def test_extraction_and_archive_limits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            markdown = root / "large.md"
            markdown.write_text("# H\n" + "x" * 100, encoding="utf-8")
            settings = make_settings(root, max_extracted_chars=20)
            with make_app(root, settings=settings) as app, self.assertRaises(ParserLimitError):
                app.ingestion.prepare_import(markdown)

            archive = root / "expanded.docx"
            archive.write_bytes(docx_bytes())
            settings = make_settings(root, max_archive_uncompressed_bytes=100)
            with make_app(root, settings=settings) as app, self.assertRaises(ParserLimitError):
                app.ingestion.prepare_import(archive)


class ParserIsolationTests(unittest.TestCase):
    def test_timeout_and_temporary_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = make_settings(root, parser_timeout_seconds=1)
            settings.ensure_private_paths()
            extractor = IsolatedExtractor(
                settings,
                command=(sys.executable, "-c", "import time; time.sleep(5)"),
            )
            with self.assertRaises(ParserTimeoutError):
                extractor.extract(b"payload", ".txt")
            self.assertEqual(list(settings.staging_dir.glob(".parse-*")), [])

    def test_abandoned_parser_file_cleanup_is_bounded_to_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = make_settings(root)
            settings.ensure_private_paths()
            stale = settings.staging_dir / ".parse-stale.txt"
            fresh = settings.staging_dir / ".parse-fresh.txt"
            unrelated = settings.staging_dir / "keep.txt"
            for path in (stale, fresh, unrelated):
                path.write_text("x", encoding="utf-8")
            old = time.time() - 3600
            os.utime(stale, (old, old))
            extractor = IsolatedExtractor(settings)
            self.assertEqual(extractor.cleanup_abandoned(), 1)
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(unrelated.exists())

    def test_parser_subprocess_does_not_inherit_service_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = make_settings(root)
            settings.ensure_private_paths()
            code = (
                "import json,os,sys; sys.stdin.read(); "
                "sys.exit(9) if os.getenv('GRAPHMIND_SECRET_PROBE') else "
                "print(json.dumps({'ok':True,'media_type':'text/plain',"
                "'extractor_fingerprint':'probe-v1','segments':[{'text':'safe','locator':'line 1'}]}))"
            )
            previous = os.environ.get("GRAPHMIND_SECRET_PROBE")
            os.environ["GRAPHMIND_SECRET_PROBE"] = "must-not-leak"
            try:
                result = IsolatedExtractor(settings, command=(sys.executable, "-c", code)).extract(
                    b"payload", ".txt"
                )
            finally:
                if previous is None:
                    os.environ.pop("GRAPHMIND_SECRET_PROBE", None)
                else:
                    os.environ["GRAPHMIND_SECRET_PROBE"] = previous
            self.assertEqual(result.segments[0].text, "safe")


class LifecycleTests(unittest.TestCase):
    def test_failed_reindex_keeps_old_version_active_and_config_change_is_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "replace.md"
            source.write_text("# V1\nOld active content.", encoding="utf-8")
            settings = make_settings(root)
            with make_app(root, settings=settings) as app:
                first = run_submission(app, source)
                changed = replace(settings, chunk_size=64, chunk_overlap=8)
                app.ingestion.settings = changed
                app.ingestion.extractor = IsolatedExtractor(changed)

                def fail(point: str):
                    if point == "after_graph":
                        raise PublicationError("injected replacement failure")

                app.ingestion.failure_injector = fail
                replacement = app.ingestion.prepare_import(source)
                self.assertNotEqual(first.version.version_id, replacement.version.version_id)
                with self.assertRaises(PublicationError):
                    app.jobs.run_once()
                self.assertEqual(
                    app.metadata.document(first.document.document_id).active_version_id,
                    first.version.version_id,
                )
                app.ingestion.failure_injector = None
                app.metadata.retry_job(replacement.job.job_id)
                app.jobs.run_once()
                self.assertEqual(
                    app.metadata.document(first.document.document_id).active_version_id,
                    replacement.version.version_id,
                )

    def test_tombstone_hides_immediately_failed_cleanup_retries_and_reimport_restores(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "delete.txt"
            source.write_text("Sensitive stale answer marker.", encoding="utf-8")
            with make_app(root) as app:
                imported = run_submission(app, source)

                def fail(point: str):
                    if point == "after_delete_vector":
                        raise PublicationError("injected cleanup failure")

                app.ingestion.failure_injector = fail
                deletion = app.ingestion.prepare_delete(imported.document.document_id)
                tombstoned = app.metadata.document(imported.document.document_id)
                self.assertIsNotNone(tombstoned.deleted_at)
                self.assertIsNone(tombstoned.active_version_id)
                self.assertEqual(app.metadata.active_version_ids(), set())
                self.assertEqual(app.retrieval.retrieve("Sensitive stale answer marker"), [])
                with self.assertRaises(DocumentBusyError):
                    app.ingestion.prepare_import(source)
                with self.assertRaises(PublicationError):
                    app.jobs.run_once()
                failed = app.metadata.job(deletion.job.job_id)
                self.assertEqual(failed.progress, 35)
                self.assertTrue((app.settings.data_dir / imported.version.private_file_ref).exists())

                app.ingestion.failure_injector = None
                app.metadata.retry_job(deletion.job.job_id)
                app.jobs.run_once()
                finished = app.metadata.job(deletion.job.job_id)
                self.assertEqual(finished.progress, 100)
                self.assertEqual(
                    app.vector.count_version(app.installation_id, imported.version.version_id), 0
                )
                self.assertEqual(
                    app.graph.count_version(app.installation_id, imported.version.version_id), 0
                )
                self.assertFalse((app.settings.data_dir / "files" / imported.document.document_id).exists())

                restored = run_submission(app, source)
                self.assertEqual(restored.version.version_id, imported.version.version_id)
                self.assertEqual(
                    app.metadata.document(imported.document.document_id).active_version_id,
                    imported.version.version_id,
                )
                self.assertGreater(
                    app.vector.count_version(app.installation_id, imported.version.version_id), 0
                )

    def test_delete_cancels_queued_replacement_and_running_publish_cannot_resurrect(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "race.txt"
            source.write_text("version one", encoding="utf-8")
            with make_app(root) as app:
                first = run_submission(app, source)
                source.write_text("queued version two", encoding="utf-8")
                queued = app.ingestion.prepare_import(source)
                deletion = app.ingestion.prepare_delete(first.document.document_id)
                cancelled = app.metadata.job(queued.job.job_id)
                self.assertEqual(cancelled.status.value, "failed")
                self.assertEqual(cancelled.error_code, "superseded_by_delete")
                app.jobs.run_once()
                self.assertEqual(app.metadata.job(deletion.job.job_id).status.value, "succeeded")
                self.assertIsNotNone(app.metadata.document(first.document.document_id).deleted_at)

                restored = run_submission(app, source)
                source.write_text("running version three", encoding="utf-8")
                running = app.ingestion.prepare_import(source)
                self.assertTrue(app.metadata.acquire_writer("manual-race", 10))
                claimed = app.metadata.claim_next_job("manual-race", 10)
                self.assertEqual(claimed.job_id, running.job.job_id)
                deletion = app.ingestion.prepare_delete(restored.document.document_id)
                with self.assertRaises(ManifestUnavailableError):
                    app.ingestion.process_job(claimed)
                self.assertIsNotNone(app.metadata.document(restored.document.document_id).deleted_at)
                app.metadata.finish_job(
                    claimed.job_id,
                    "manual-race",
                    succeeded=False,
                    error_code="manifest_unavailable",
                )
                app.metadata.release_writer("manual-race")
                app.jobs.run_once()
                self.assertEqual(app.metadata.job(deletion.job.job_id).status.value, "succeeded")
                self.assertEqual(app.metadata.active_version_ids(), set())

    def test_concurrent_delete_is_idempotent_and_queue_failure_does_not_tombstone(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_path = root / "first.txt"
            first_path.write_text("first", encoding="utf-8")
            settings = make_settings(root, max_queued_jobs=1)
            with make_app(root, settings=settings) as app:
                first = run_submission(app, first_path)
                queued_path = root / "queued.txt"
                queued_path.write_text("queued", encoding="utf-8")
                app.ingestion.prepare_import(queued_path)
                with self.assertRaises(JobQueueFullError):
                    app.ingestion.prepare_delete(first.document.document_id)
                self.assertIsNone(app.metadata.document(first.document.document_id).deleted_at)
                app.jobs.run_once()

                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(
                        pool.map(
                            lambda _: app.ingestion.prepare_delete(first.document.document_id),
                            range(2),
                        )
                    )
                self.assertEqual(results[0].job.job_id, results[1].job.job_id)
                delete_jobs = [
                    job
                    for job in app.metadata.list_jobs()
                    if job.operation == "delete_document" and job.document_id == first.document.document_id
                ]
                self.assertEqual(len(delete_jobs), 1)

    def test_queued_delete_resumes_after_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = make_settings(root)
            vector = MemoryVectorStore(HashEmbedding(settings.embedding_dimension))
            graph = MemoryGraphStore()
            source = root / "restart.txt"
            source.write_text("restart cleanup", encoding="utf-8")
            first_app = make_app(root, settings=settings, vector=vector, graph=graph)
            imported = run_submission(first_app, source)
            deletion = first_app.ingestion.prepare_delete(imported.document.document_id)
            first_app.close()

            with make_app(root, settings=settings, vector=vector, graph=graph) as restarted:
                restarted.jobs.reconcile()
                restarted.jobs.run_once()
                self.assertEqual(
                    restarted.metadata.job(deletion.job.job_id).status.value, "succeeded"
                )
                self.assertEqual(
                    vector.count_version(restarted.installation_id, imported.version.version_id), 0
                )


if __name__ == "__main__":
    unittest.main()
