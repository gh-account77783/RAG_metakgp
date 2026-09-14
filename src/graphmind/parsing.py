"""Bounded subprocess extraction for supported document formats."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .config import Settings
from .errors import (
    DocumentEncodingError,
    DocumentParseError,
    DocumentTooLargeError,
    EmptyDocumentError,
    EncryptedDocumentError,
    OcrRequiredError,
    ParserLimitError,
    ParserTimeoutError,
    UnsupportedFileError,
)


SUPPORTED_MEDIA_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".csv": "text/csv",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


@dataclass(frozen=True, slots=True)
class ExtractedSegment:
    text: str
    locator: str
    line_start: int | None = None


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    media_type: str
    extractor_fingerprint: str
    segments: tuple[ExtractedSegment, ...]
    warnings: tuple[str, ...] = ()


_ERROR_TYPES = {
    "document_encoding_error": DocumentEncodingError,
    "document_parse_error": DocumentParseError,
    "document_too_large": DocumentTooLargeError,
    "empty_document": EmptyDocumentError,
    "encrypted_document": EncryptedDocumentError,
    "ocr_required": OcrRequiredError,
    "parser_limit": ParserLimitError,
}


class IsolatedExtractor:
    def __init__(self, settings: Settings, *, command: Sequence[str] | None = None) -> None:
        self.settings = settings
        self.command = tuple(command or (sys.executable, "-m", "graphmind.parser_worker"))

    def cleanup_abandoned(self) -> int:
        self.settings.staging_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        cutoff = time.time() - max(60, self.settings.parser_timeout_seconds * 2)
        removed = 0
        for path in self.settings.staging_dir.glob(".parse-*"):
            try:
                resolved = path.resolve()
                resolved.relative_to(self.settings.staging_dir.resolve())
                if resolved.is_file() and resolved.stat().st_mtime < cutoff:
                    resolved.unlink()
                    removed += 1
            except (OSError, ValueError):
                continue
        return removed

    @staticmethod
    def _worker_environment() -> dict[str, str]:
        """Give the parser only process-launch essentials, never service credentials."""
        allowed = {
            "HOME",
            "LOCALAPPDATA",
            "PATH",
            "PATHEXT",
            "PYTHONPATH",
            "SYSTEMDRIVE",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "USERPROFILE",
            "VIRTUAL_ENV",
            "WINDIR",
        }
        environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONNOUSERSITE"] = "1"
        return environment

    def extract(self, content: bytes, suffix: str) -> ExtractionResult:
        normalized_suffix = suffix.casefold()
        if normalized_suffix not in SUPPORTED_MEDIA_TYPES:
            raise UnsupportedFileError(
                "Supported formats are PDF, DOCX, TXT, Markdown, and CSV"
            )
        if not content:
            raise EmptyDocumentError("Input file is empty")
        if len(content) > self.settings.max_file_bytes:
            raise DocumentTooLargeError(
                f"Input exceeds the {self.settings.max_file_bytes}-byte limit"
            )

        self.settings.staging_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.settings.staging_dir / f".parse-{uuid.uuid4().hex}{normalized_suffix}"
        try:
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
            request = {
                "path": str(temporary),
                "suffix": normalized_suffix,
                "limits": {
                    "max_extracted_chars": self.settings.max_extracted_chars,
                    "max_archive_entries": self.settings.max_archive_entries,
                    "max_archive_uncompressed_bytes": self.settings.max_archive_uncompressed_bytes,
                    "max_pdf_pages": self.settings.max_pdf_pages,
                },
                "csv_delimiter": self.settings.csv_delimiter,
            }
            try:
                completed = subprocess.run(
                    self.command,
                    input=json.dumps(request),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.settings.parser_timeout_seconds,
                    check=False,
                    env=self._worker_environment(),
                )
            except subprocess.TimeoutExpired as exc:
                raise ParserTimeoutError(
                    f"Document extraction exceeded {self.settings.parser_timeout_seconds} seconds"
                ) from exc
            if completed.returncode != 0:
                raise DocumentParseError("The isolated document parser failed")
            if len(completed.stdout) > self.settings.max_extracted_chars * 2 + 100_000:
                raise ParserLimitError("Document parser output exceeded its configured limit")
            try:
                payload = json.loads(completed.stdout)
            except (TypeError, json.JSONDecodeError) as exc:
                raise DocumentParseError("The isolated document parser returned invalid output") from exc
            if not isinstance(payload, dict):
                raise DocumentParseError("The isolated document parser returned invalid output")
            if not payload.get("ok"):
                code = str(payload.get("code", "document_parse_error"))
                error_type = _ERROR_TYPES.get(code, DocumentParseError)
                message = str(payload.get("message") or "Document extraction failed")
                raise error_type(message)
            try:
                segments = tuple(
                    ExtractedSegment(
                        text=str(item["text"]),
                        locator=str(item["locator"]),
                        line_start=(
                            int(item["line_start"])
                            if item.get("line_start") is not None
                            else None
                        ),
                    )
                    for item in payload["segments"]
                )
                result = ExtractionResult(
                    media_type=str(payload["media_type"]),
                    extractor_fingerprint=str(payload["extractor_fingerprint"]),
                    segments=segments,
                    warnings=tuple(str(item) for item in payload.get("warnings", [])),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise DocumentParseError("The isolated document parser returned invalid output") from exc
            if not result.segments or not any(segment.text.strip() for segment in result.segments):
                raise EmptyDocumentError("Input contains no extractable text")
            if sum(len(segment.text) for segment in result.segments) > self.settings.max_extracted_chars:
                raise ParserLimitError("Extracted text exceeded its configured limit")
            return result
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
