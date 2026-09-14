"""Subprocess worker for document text extraction.

The worker reads one small JSON request from stdin and writes one JSON result to
stdout. It never opens URLs, executes document content, or mutates application
stores.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree


class WorkerFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _normalize(text: str) -> str:
    return unicodedata.normalize(
        "NFC", text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    )


def _decode_utf8(path: Path) -> str:
    try:
        return _normalize(path.read_bytes().decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise WorkerFailure("document_encoding_error", "Text input must use UTF-8 encoding") from exc
    except OSError as exc:
        raise WorkerFailure("document_parse_error", "Document content could not be read") from exc


def _check_text_limit(segments: list[dict[str, object]], limit: int) -> None:
    if sum(len(str(segment["text"])) for segment in segments) > limit:
        raise WorkerFailure("parser_limit", "Extracted text exceeded its configured limit")


def _extract_txt(path: Path, limits: dict[str, int]) -> dict[str, object]:
    text = _decode_utf8(path)
    if not text.strip():
        raise WorkerFailure("empty_document", "Input contains no text")
    segments = [{"text": text, "locator": "text", "line_start": 1}]
    _check_text_limit(segments, limits["max_extracted_chars"])
    return {
        "media_type": "text/plain",
        "extractor_fingerprint": "txt-utf8-nfc-v1",
        "segments": segments,
        "warnings": [],
    }


def _heading_context(heading: str | None) -> str:
    return f' under "{heading}"' if heading else ""


def _extract_markdown(path: Path, limits: dict[str, int]) -> dict[str, object]:
    text = _decode_utf8(path)
    if not text.strip():
        raise WorkerFailure("empty_document", "Markdown input contains no text")
    lines = text.splitlines()
    segments: list[dict[str, object]] = []
    heading: str | None = None
    index = 0
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        if not stripped:
            index += 1
            continue
        heading_match = re.match(r"^(#{1,6})\s+(.+?)\s*#*$", stripped)
        if heading_match:
            heading = heading_match.group(2).strip()
            segments.append(
                {"text": heading, "locator": f'heading "{heading}" line {index + 1}'}
            )
            index += 1
            continue
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            start = index
            block = [raw]
            index += 1
            while index < len(lines):
                block.append(lines[index])
                if lines[index].strip().startswith(marker):
                    index += 1
                    break
                index += 1
            segments.append(
                {
                    "text": "\n".join(block),
                    "locator": f"code block lines {start + 1}-{index}{_heading_context(heading)}",
                }
            )
            continue
        if "|" in raw:
            start = index
            block: list[str] = []
            while index < len(lines) and lines[index].strip() and "|" in lines[index]:
                block.append(lines[index])
                index += 1
            segments.append(
                {
                    "text": "\n".join(block),
                    "locator": f"table lines {start + 1}-{index}{_heading_context(heading)}",
                }
            )
            continue
        start = index
        block = []
        while index < len(lines):
            candidate = lines[index]
            value = candidate.strip()
            if not value or value.startswith(("```", "~~~")) or re.match(
                r"^#{1,6}\s+", value
            ):
                break
            if "|" in candidate and block:
                break
            block.append(candidate)
            index += 1
        if block:
            end = start + len(block)
            segments.append(
                {
                    "text": "\n".join(block),
                    "locator": f"lines {start + 1}-{end}{_heading_context(heading)}",
                    "line_start": start + 1,
                }
            )
        else:
            index += 1
    _check_text_limit(segments, limits["max_extracted_chars"])
    return {
        "media_type": "text/markdown",
        "extractor_fingerprint": "markdown-utf8-structural-v1",
        "segments": segments,
        "warnings": ["raw_html_is_untrusted_text", "external_links_are_not_fetched"],
    }


def _valid_header(header: list[str]) -> bool:
    normalized = [value.strip() for value in header]
    if not normalized or any(not value for value in normalized):
        return False
    if len(set(value.casefold() for value in normalized)) != len(normalized):
        return False
    return not all(re.fullmatch(r"[-+]?\d+(?:\.\d+)?", value) for value in normalized)


def _extract_csv(
    path: Path, limits: dict[str, int], delimiter_setting: str = "auto"
) -> dict[str, object]:
    text = _decode_utf8(path)
    if not text.strip():
        raise WorkerFailure("empty_document", "CSV input contains no text")
    declared = {"comma": ",", "semicolon": ";", "tab": "\t", "pipe": "|"}
    delimiter = declared.get(delimiter_setting)
    if delimiter is None:
        try:
            dialect = csv.Sniffer().sniff(text[:65536], delimiters=",;\t|")
            delimiter = dialect.delimiter
        except csv.Error:
            delimiter = ","
    try:
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
        header = next(reader)
    except (StopIteration, csv.Error) as exc:
        raise WorkerFailure("document_parse_error", "CSV structure is invalid") from exc
    if not _valid_header(header):
        raise WorkerFailure(
            "document_parse_error", "CSV requires a non-empty, unique header row"
        )
    if len(header) > 512:
        raise WorkerFailure("parser_limit", "CSV column count exceeds 512")
    header = [value.strip() for value in header]
    segments: list[dict[str, object]] = []
    try:
        for logical_row, row in enumerate(reader, start=2):
            if not row or not any(value.strip() for value in row):
                continue
            if len(row) != len(header):
                raise WorkerFailure(
                    "document_parse_error",
                    f"CSV row {logical_row} has {len(row)} columns; expected {len(header)}",
                )
            values = [_normalize(value) for value in row]
            rendered = " | ".join(f"{name}: {value}" for name, value in zip(header, values))
            columns = ", ".join(header)
            if len(columns) > 240:
                columns = columns[:237] + "..."
            segments.append(
                {
                    "text": rendered,
                    "locator": f"row {logical_row} columns {columns}",
                }
            )
            _check_text_limit(segments, limits["max_extracted_chars"])
    except csv.Error as exc:
        raise WorkerFailure("document_parse_error", "CSV structure is invalid") from exc
    if not segments:
        raise WorkerFailure("empty_document", "CSV contains a header but no data rows")
    delimiter_name = {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}.get(
        delimiter, "other"
    )
    return {
        "media_type": "text/csv",
        "extractor_fingerprint": f"csv-utf8-v1:delimiter={delimiter_name}",
        "segments": segments,
        "warnings": ["spreadsheet_formulas_are_inert_text"],
    }


_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _xml_text(element: ElementTree.Element) -> str:
    values: list[str] = []
    for node in element.iter():
        if node.tag == _WORD_NS + "t" and node.text:
            values.append(node.text)
        elif node.tag == _WORD_NS + "tab":
            values.append("\t")
        elif node.tag in {_WORD_NS + "br", _WORD_NS + "cr"}:
            values.append("\n")
    return _normalize("".join(values)).strip()


def _safe_docx_archive(archive: zipfile.ZipFile, limits: dict[str, int]) -> None:
    infos = archive.infolist()
    if len(infos) > limits["max_archive_entries"]:
        raise WorkerFailure("parser_limit", "DOCX archive has too many entries")
    total = 0
    for info in infos:
        name = PurePosixPath(info.filename)
        if name.is_absolute() or ".." in name.parts:
            raise WorkerFailure("document_parse_error", "DOCX archive contains an unsafe path")
        if info.flag_bits & 0x1:
            raise WorkerFailure("encrypted_document", "Encrypted DOCX files are not supported")
        total += info.file_size
        if total > limits["max_archive_uncompressed_bytes"]:
            raise WorkerFailure("parser_limit", "DOCX expanded size exceeds its configured limit")
        if info.file_size > 0 and info.compress_size == 0:
            raise WorkerFailure("parser_limit", "DOCX entry has an invalid compression ratio")
        if info.compress_size and info.file_size / info.compress_size > 200:
            raise WorkerFailure("parser_limit", "DOCX entry compression ratio exceeds 200")


def _extract_docx(path: Path, limits: dict[str, int]) -> dict[str, object]:
    try:
        with zipfile.ZipFile(path) as archive:
            _safe_docx_archive(archive, limits)
            try:
                xml = archive.read("word/document.xml")
            except KeyError as exc:
                raise WorkerFailure(
                    "document_parse_error", "DOCX is missing word/document.xml"
                ) from exc
    except WorkerFailure:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise WorkerFailure("document_parse_error", "DOCX archive is invalid") from exc
    lowered = xml.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise WorkerFailure("document_parse_error", "DOCX XML declarations are not allowed")
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise WorkerFailure("document_parse_error", "DOCX document XML is invalid") from exc
    body = root.find(f".//{_WORD_NS}body")
    if body is None:
        raise WorkerFailure("document_parse_error", "DOCX document body is missing")
    segments: list[dict[str, object]] = []
    paragraph_number = 0
    table_number = 0
    for child in body:
        if child.tag == _WORD_NS + "p":
            paragraph_number += 1
            text = _xml_text(child)
            if text:
                style = child.find(f"./{_WORD_NS}pPr/{_WORD_NS}pStyle")
                style_name = style.get(_WORD_NS + "val") if style is not None else None
                suffix = f" ({style_name})" if style_name else ""
                segments.append(
                    {"text": text, "locator": f"paragraph {paragraph_number}{suffix}"}
                )
        elif child.tag == _WORD_NS + "tbl":
            table_number += 1
            for row_number, row in enumerate(child.findall(f"./{_WORD_NS}tr"), start=1):
                cells = [_xml_text(cell) for cell in row.findall(f"./{_WORD_NS}tc")]
                if any(cells):
                    rendered = " | ".join(
                        f"column {number}: {value}"
                        for number, value in enumerate(cells, start=1)
                    )
                    segments.append(
                        {
                            "text": rendered,
                            "locator": (
                                f"table {table_number} row {row_number} "
                                f"columns 1-{len(cells)}"
                            ),
                        }
                    )
        _check_text_limit(segments, limits["max_extracted_chars"])
    if not segments:
        raise WorkerFailure("empty_document", "DOCX contains no extractable text")
    return {
        "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "extractor_fingerprint": "docx-openxml-bounded-v1",
        "segments": segments,
        "warnings": ["embedded_objects_and_external_relationships_are_not_processed"],
    }


def _extract_pdf(path: Path, limits: dict[str, int]) -> dict[str, object]:
    try:
        import pypdf

        reader = pypdf.PdfReader(path, strict=True)
        if reader.is_encrypted:
            raise WorkerFailure("encrypted_document", "Encrypted PDF files are not supported")
        if len(reader.pages) == 0:
            raise WorkerFailure("empty_document", "PDF contains no pages")
        if len(reader.pages) > limits["max_pdf_pages"]:
            raise WorkerFailure("parser_limit", "PDF page count exceeds its configured limit")
        segments: list[dict[str, object]] = []
        for page_number, page in enumerate(reader.pages, start=1):
            text = _normalize(page.extract_text() or "").strip()
            if text:
                segments.append({"text": text, "locator": f"page {page_number}"})
                _check_text_limit(segments, limits["max_extracted_chars"])
    except WorkerFailure:
        raise
    except Exception as exc:
        raise WorkerFailure("document_parse_error", "PDF structure is invalid") from exc
    if not segments:
        raise WorkerFailure(
            "ocr_required", "PDF has pages but no extractable text; OCR is not supported"
        )
    return {
        "media_type": "application/pdf",
        "extractor_fingerprint": f"pdf-pypdf-{pypdf.__version__}-text-v1",
        "segments": segments,
        "warnings": ["pdf_layout_and_reading_order_may_not_be_preserved", "ocr_is_not_supported"],
    }


def _extract(request: dict[str, object]) -> dict[str, object]:
    try:
        path = Path(str(request["path"]))
        suffix = str(request["suffix"]).casefold()
        raw_limits = request["limits"]
        if not isinstance(raw_limits, dict):
            raise TypeError
        limits = {str(key): int(value) for key, value in raw_limits.items()}
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkerFailure("document_parse_error", "Parser request is invalid") from exc
    extractors = {
        ".txt": _extract_txt,
        ".md": _extract_markdown,
        ".markdown": _extract_markdown,
        ".docx": _extract_docx,
        ".pdf": _extract_pdf,
    }
    if suffix == ".csv":
        return _extract_csv(path, limits, str(request.get("csv_delimiter", "auto")))
    extractor = extractors.get(suffix)
    if extractor is None:
        raise WorkerFailure("document_parse_error", "Document format is unsupported")
    return extractor(path, limits)


def main() -> None:
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict):
            raise WorkerFailure("document_parse_error", "Parser request is invalid")
        result = _extract(request)
        response = {"ok": True, **result}
    except WorkerFailure as exc:
        response = {"ok": False, "code": exc.code, "message": str(exc)}
    except Exception:
        response = {
            "ok": False,
            "code": "document_parse_error",
            "message": "Document extraction failed",
        }
    # ASCII JSON is portable across Windows code pages; the parent decodes it as UTF-8.
    sys.stdout.write(json.dumps(response, ensure_ascii=True))


if __name__ == "__main__":
    main()
