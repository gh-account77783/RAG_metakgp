"""Small local-only adapter around the pinned upstream PageIndex library."""
from pathlib import Path
import tempfile

from ..components.local_inference import LocalInference


class PageIndexAdapter:
    def __init__(self, inference: LocalInference, staging_dir: Path):
        self.inference, self.staging_dir = inference, staging_dir

    def extract(self, original: bytes) -> list[dict]:
        self.staging_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix=".tree-", dir=self.staging_dir) as temporary:
            path = Path(temporary) / "original.pdf"
            path.write_bytes(original)
            return self.inference.run("tree", {"pdf_path": str(path)})["structure"]
