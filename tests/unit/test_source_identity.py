"""Original-file identity must include changes outside extracted text."""
import hashlib
import io
from pathlib import Path
import tempfile
import unittest

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from tests.unit.test_ingestion_lifecycle import make_app, pdf_bytes, run_submission


def illustrated_pdf(color: str) -> bytes:
    reader = PdfReader(io.BytesIO(pdf_bytes(["The chart appears below this sentence."])))
    writer = PdfWriter()
    writer.append_pages_from_reader(reader)
    page = writer.pages[0]
    content = page.get_contents().get_data()
    stream = DecodedStreamObject()
    stream.set_data(content + f"\nq {color} rg 72 400 200 50 re f Q\n".encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    target = io.BytesIO()
    writer.write(target)
    return target.getvalue()


class SourceIdentityTests(unittest.TestCase):
    def test_visual_only_pdf_replacement_creates_a_new_immutable_source_version(self):
        first_bytes, second_bytes = illustrated_pdf("1 0 0"), illustrated_pdf("0 0 1")
        self.assertNotEqual(first_bytes, second_bytes)
        self.assertEqual(PdfReader(io.BytesIO(first_bytes)).pages[0].extract_text(),
                         PdfReader(io.BytesIO(second_bytes)).pages[0].extract_text())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "chart.pdf"
            source.write_bytes(first_bytes)
            with make_app(root) as app:
                first = run_submission(app, source)
                source.write_bytes(second_bytes)
                replacement = app.ingestion.prepare_import(source)
                self.assertNotEqual(first.version.version_id, replacement.version.version_id)
                self.assertEqual(app.metadata.document(first.document.document_id).active_version_id,
                                 first.version.version_id)
                self.assertEqual((app.settings.data_dir / first.version.private_file_ref).read_bytes(), first_bytes)
                app.jobs.run_job(replacement.job.job_id)
                self.assertEqual(app.metadata.document(first.document.document_id).active_version_id,
                                 replacement.version.version_id)
                self.assertEqual((app.settings.data_dir / replacement.version.private_file_ref).read_bytes(),
                                 second_bytes)
                self.assertEqual(first.version.content_hash, hashlib.sha256(first_bytes).hexdigest())
                self.assertEqual(replacement.version.content_hash, hashlib.sha256(second_bytes).hexdigest())
                repeated = app.ingestion.prepare_import(source)
                self.assertEqual(repeated.version.version_id, replacement.version.version_id)
                self.assertEqual(repeated.job.job_id, replacement.job.job_id)


if __name__ == "__main__":
    unittest.main()
