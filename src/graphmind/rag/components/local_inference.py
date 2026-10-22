"""Bounded host-local workers; model weights leave memory when a call finishes."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys

from ...errors import DependencyUnavailableError, ModelNotFoundError, QueryCapacityError
from ...locking import exclusive_file_lock
from ...parsing import IsolatedExtractor
from .contracts import AdapterStatus


def verify_model(directory: Path, expected_model: str) -> dict:
    """Use only the explicitly retained, complete safetensors artifact inventory."""
    try:
        manifest_path = directory / ".graphmind-model.json"
        if directory.is_symlink() or manifest_path.is_symlink():
            raise ValueError("Model path changed")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (manifest["schema_version"] != 1 or manifest["model"] != expected_model
                or not re.fullmatch(r"[0-9a-f]{40}", manifest["revision"])):
            raise ValueError("Model identity differs")
        names = {item["filename"] for item in manifest["files"]}
        if (len(names) != len(manifest["files"]) or not any(name.endswith(".safetensors") for name in names)
                or {item.name for item in directory.iterdir()} != names | {manifest_path.name}):
            raise ValueError("Model inventory differs")
        for item in manifest["files"]:
            name = item["filename"]
            if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", name)
                    or name.endswith((".py", ".bin", ".pt", ".pth", ".pkl"))):
                raise ValueError("Unsafe model artifact")
            path = directory / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size != item["size"]:
                raise ValueError("Model artifact differs")
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != item["sha256"]:
                    raise ValueError("Model bytes differ")
        return manifest
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ModelNotFoundError("Configured local model is missing or fails artifact verification") from exc


class LocalInference:
    def __init__(self, data_dir: Path, *, timeout: float = 240, threads: int = 2):
        self.lock_path = data_dir / ".inference.lock"
        self.timeout = timeout
        self.threads = threads

    def run(self, operation: str, payload: dict, *, timeout: float | None = None) -> dict:
        limit = self.timeout if timeout is None else timeout
        if not 1 <= limit <= 1800:
            raise ValueError("Local inference timeout must be between 1 and 1800 seconds")
        request = json.dumps({"operation": operation, "threads": self.threads, **payload}, allow_nan=False)
        if len(request.encode("utf-8")) > 4 * 1024 * 1024:
            raise DependencyUnavailableError("Local inference input exceeds its limit")
        with exclusive_file_lock(self.lock_path) as acquired:
            if not acquired:
                raise QueryCapacityError("Another local inference operation is running; retry")
            environment = IsolatedExtractor._worker_environment()
            environment.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                                "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1", "TOKENIZERS_PARALLELISM": "false",
                                "OMP_NUM_THREADS": str(self.threads), "MKL_NUM_THREADS": str(self.threads)})
            try:
                result = subprocess.run([sys.executable, "-B", "-m", "graphmind.rag.components.model_worker"],
                    input=request, text=True, encoding="utf-8", capture_output=True,
                    timeout=limit, check=False, env=environment)
                if result.returncode or len(result.stdout) > 8 * 1024 * 1024:
                    raise ValueError("Worker failed")
                output = json.loads(result.stdout)
                if not isinstance(output, dict) or output.get("ok") is not True:
                    raise ValueError("Invalid worker output")
                return output
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                raise DependencyUnavailableError("Local inference failed or exceeded its time limit") from exc


class LocalCrossEncoder:
    def __init__(self, directory: Path, inference: LocalInference):
        self.directory = directory
        self.inference = inference

    def identity(self) -> dict:
        manifest = verify_model(self.directory, "BAAI/bge-reranker-v2-m3")
        return {"model": manifest["model"], "revision": manifest["revision"], "device": "cpu",
                "execution": "bounded subprocess", "weights": "safetensors"}

    def readiness(self) -> AdapterStatus:
        available = (self.directory / ".graphmind-model.json").is_file()
        return AdapterStatus("local-reranker", available,
                             "configured; verified by worker before use" if available else "model missing")

    def score(self, question: str, passages: list[str]) -> list[float]:
        return self.score_pairs([(question, passage) for passage in passages])

    def score_pairs(self, pairs: list[tuple[str, str]]) -> list[float]:
        if not pairs:
            return []
        if len(pairs) > 50:
            raise ValueError("Rerank pool exceeds its limit")
        output = self.inference.run("rerank", {"model_dir": str(self.directory),
                                               "pairs": pairs})
        try:
            scores = [float(value) for value in output["scores"]]
            if len(scores) != len(pairs) or any(not math.isfinite(value) for value in scores):
                raise ValueError("Invalid scores")
            return scores
        except (KeyError, TypeError, ValueError) as exc:
            raise DependencyUnavailableError("Local reranker returned invalid scores") from exc
