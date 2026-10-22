"""Thin, offline Sentence Transformers multi-vector page adapter."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import tempfile

from ...errors import DependencyUnavailableError, ModelNotFoundError
from ..components.local_inference import verify_model


MODEL = "vidore/colqwen2-v1.0-hf"
REVISION = "ddc07d2317c80f75fc742b7362ee9ad1912908f9"


def require_libraries():
    from importlib.metadata import PackageNotFoundError, version
    try:
        expected = {"sentence-transformers": "6.1.0", "torch": "2.14.1+cpu",
                    "torchvision": "0.29.1+cpu", "transformers": "5.20.2", "pypdfium2": "5.14.0"}
        if any(version(name) != pinned for name, pinned in expected.items()):
            raise DependencyUnavailableError("Visual dependencies differ from the qualified CPU profile")
    except PackageNotFoundError as exc:
        raise DependencyUnavailableError("Install the qualified visual dependency profile") from exc


class ColQwenAdapter:
    def __init__(self, directory: Path, inference, staging_dir: Path, *, build_timeout=900):
        self.directory, self.inference, self.staging_dir = directory, inference, staging_dir
        self.build_timeout = build_timeout

    def identity(self):
        require_libraries()
        # Reading metadata is cheap; the worker verifies every retained model byte.
        try:
            path = self.directory / ".graphmind-model.json"
            raw = path.read_bytes()
            manifest = json.loads(raw)
            if path.is_symlink() or self.directory.is_symlink() or manifest["model"] != MODEL or manifest["revision"] != REVISION:
                raise ValueError("Visual model identity differs")
            return {"model": MODEL, "revision": REVISION, "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                    "sentence_transformers": "6.1.0", "device": "cpu", "dtype": "bfloat16"}
        except (OSError, KeyError, ValueError, TypeError) as exc:
            raise ModelNotFoundError("Configure the pinned, retained ColQwen2 model directory") from exc

    def _temporary(self):
        self.staging_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return tempfile.TemporaryDirectory(prefix=".visual-", dir=self.staging_dir)

    def build(self, images: dict[str, bytes]) -> bytes:
        with self._temporary() as temporary:
            directory = Path(temporary)
            for name, content in images.items():
                (directory / name).write_bytes(content)
            path = directory / "vectors.safetensors"
            result = self.inference.run("visual_build", {"model_dir": str(self.directory),
                "image_paths": [str(directory / name) for name in images], "vector_path": str(path)},
                timeout=self.build_timeout)
            if result.get("page_count") != len(images) or path.is_symlink():
                raise DependencyUnavailableError("Visual worker returned a different page inventory")
            with path.open("rb") as stream:
                content = stream.read(256 * 1024 * 1024 + 1)
            if not content or len(content) > 256 * 1024 * 1024:
                raise DependencyUnavailableError("Visual vectors exceed their byte limit")
            return content

    def rank(self, question: str, vectors: bytes, *, page_count: int) -> list[tuple[int, float]]:
        with self._temporary() as temporary:
            path = Path(temporary) / "vectors.safetensors"
            path.write_bytes(vectors)
            result = self.inference.run("visual_rank", {"model_dir": str(self.directory),
                "question": question, "vector_path": str(path), "page_count": page_count})
            try:
                scores = [float(value) for value in result["scores"]]
                if len(scores) != page_count or any(not math.isfinite(value) for value in scores):
                    raise ValueError("Invalid visual scores")
                return sorted(enumerate(scores, 1), key=lambda pair: (-pair[1], pair[0]))[:3]
            except (KeyError, ValueError, TypeError) as exc:
                raise DependencyUnavailableError("Visual worker returned invalid scores") from exc


def visual_worker(request: dict) -> dict:
    require_libraries()
    import torch
    from sentence_transformers import MultiVectorEncoder
    from safetensors.torch import load_file, save_file
    from PIL import Image
    directory = Path(request["model_dir"])
    manifest = verify_model(directory, MODEL)
    if manifest["revision"] != REVISION:
        raise ValueError("Visual model revision differs")
    model = MultiVectorEncoder(str(directory), device="cpu", local_files_only=True,
        trust_remote_code=False, token=False, model_kwargs={"dtype": torch.bfloat16, "use_safetensors": True})
    with torch.inference_mode():
        if request["operation"] == "visual_build":
            paths = request["image_paths"]
            if not 1 <= len(paths) <= 50:
                raise ValueError("Visual page budget")
            vectors = {}
            for number, path in enumerate(paths, 1):
                with Image.open(path) as image:
                    if image.format != "PNG" or image.width > 1280 or image.height > 1800:
                        raise ValueError("Visual raster bounds")
                    value = model.encode_document([image.convert("RGB")], batch_size=1, show_progress_bar=False)[0]
                vectors[str(number)] = value.detach().to(device="cpu").contiguous()
            save_file(vectors, request["vector_path"])
            return {"ok": True, "page_count": len(vectors)}
        vectors = load_file(request["vector_path"], device="cpu")
        count = request["page_count"]
        if (type(count) is not int or not 1 <= count <= 50
                or set(vectors) != {str(number) for number in range(1, count + 1)}):
            raise ValueError("Visual vector inventory differs")
        ordered = [vectors[str(number)] for number in range(1, count + 1)]
        if any(value.ndim != 2 or value.shape[0] > 8192 or value.shape[1] > 4096
               or not torch.isfinite(value).all() for value in ordered):
            raise ValueError("Invalid page vectors")
        query = model.encode_query([request["question"]], batch_size=1, show_progress_bar=False)
        scores = model.similarity(query, ordered)[0]
        return {"ok": True, "scores": scores.float().tolist()}
