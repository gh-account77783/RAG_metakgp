"""Original PDF-page identities, extracted text and bounded raster previews."""
from __future__ import annotations

from pathlib import Path
import re
import tempfile

from ...errors import ManifestUnavailableError, ParserLimitError, UnsupportedFileError
from .index_contracts import IndexInventory


def source_pages(metadata, version, *, max_pages: int) -> list[dict]:
    chunks = metadata.all_chunks_for_version(version.version_id)
    inventory = IndexInventory.from_unit_ids([item.chunk_id for item in chunks])
    if (inventory.unit_count, inventory.unit_digest) != (version.expected_chunk_count, version.chunk_digest):
        raise ManifestUnavailableError("PDF source inventory differs")
    pages = {}
    for chunk in chunks:
        match = re.fullmatch(r"page (\d+)(?:, part \d+)?", chunk.locator)
        if match is None:
            raise UnsupportedFileError("This engine requires original PDF-page text")
        page = int(match.group(1))
        if page not in range(1, max_pages + 1):
            raise ParserLimitError("PDF page exceeds the configured limit")
        existing = pages.setdefault(page, {"text": "", "start": chunk.start_offset})
        offset = chunk.start_offset - existing["start"]
        if offset > len(existing["text"]):
            existing["text"] += " " * (offset - len(existing["text"]))
        overlap = max(0, len(existing["text"]) - offset)
        existing["text"] += chunk.text[overlap:]
    return [{"page": number, "unit_id": f"page:{version.version_id}:{number}",
             "locator": f"page {number}", "text": record["text"]}
            for number, record in sorted(pages.items())]


def rasterize_pdf(original: bytes, staging_dir: Path, inference, *, max_pages=50,
                  page_numbers: tuple[int, ...] | None = None) -> dict[str, bytes]:
    staging_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".raster-", dir=staging_dir) as temporary:
        directory = Path(temporary)
        path = directory / "original.pdf"
        path.write_bytes(original)
        result = inference.run("render_pdf", {"pdf_path": str(path), "output_dir": str(directory),
                                              "max_pages": max_pages, "page_numbers": page_numbers})
        count = result.get("page_count")
        if type(count) is not int or not 1 <= count <= max_pages:
            raise ParserLimitError("PDF raster page count exceeds its limit")
        selected = tuple(range(1, count + 1)) if page_numbers is None else page_numbers
        if (not selected or len(selected) > max_pages or len(set(selected)) != len(selected)
                or any(type(number) is not int or not 1 <= number <= count for number in selected)):
            raise ValueError("Requested PDF pages exceed the source bounds")
        files, total = {}, 0
        for number in selected:
            name = f"page-{number}.png"
            target = directory / name
            if target.is_symlink():
                raise ManifestUnavailableError("PDF raster path differs")
            with target.open("rb") as stream:
                content = stream.read(8 * 1024 * 1024 + 1)
            total += len(content)
            if (not content.startswith(b"\x89PNG\r\n\x1a\n") or len(content) > 8 * 1024 * 1024
                    or total > 128 * 1024 * 1024):
                raise ParserLimitError("PDF rasters exceed their byte limit")
            files[name] = content
        return files


def render_worker(request: dict) -> dict:
    """Rasterize through PDFium; never expose an active PDF in the reader origin."""
    import pypdfium2 as pdfium
    from importlib.metadata import version
    if version("pypdfium2") != "5.14.0":
        raise ValueError("PDF raster library differs from the qualified profile")
    document = pdfium.PdfDocument(request["pdf_path"])
    try:
        if not 1 <= len(document) <= min(int(request["max_pages"]), 1000):
            raise ValueError("PDF page limit")
        selected = request.get("page_numbers")
        if selected is None:
            selected = list(range(1, len(document) + 1))
        if (not isinstance(selected, list) or not selected or len(set(selected)) != len(selected)
                or any(type(number) is not int for number in selected)):
            raise ValueError("Invalid PDF page selection")
        if any(not 1 <= number <= len(document) for number in selected):
            return {"ok": True, "page_count": len(document)}
        for selected_page in selected:
            number = selected_page - 1
            page = document[number]
            try:
                width, height = page.get_size()
                if width <= 0 or height <= 0:
                    raise ValueError("Invalid PDF page dimensions")
                scale = min(1.0, 1280 / width, 1800 / height)
                bitmap = page.render(scale=scale)
                try:
                    image = bitmap.to_pil()
                    try:
                        image.save(Path(request["output_dir"]) / f"page-{number + 1}.png", format="PNG")
                    finally:
                        image.close()
                finally:
                    bitmap.close()
            finally:
                page.close()
        return {"ok": True, "page_count": len(document)}
    finally:
        document.close()
