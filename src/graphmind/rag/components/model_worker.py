"""Internal inference subprocess; no provider credentials, hub download or code loading."""
import json
from pathlib import Path
import sys

from .local_inference import verify_model


def run(request: dict) -> dict:
    import torch
    torch.set_num_threads(int(request["threads"]))
    if request["operation"] == "rerank":
        from sentence_transformers import CrossEncoder
        directory = Path(request["model_dir"])
        verify_model(directory, "BAAI/bge-reranker-v2-m3")
        model = CrossEncoder(str(directory), device="cpu", local_files_only=True,
                             trust_remote_code=False, token=False, max_length=1024,
                             model_kwargs={"use_safetensors": True})
        with torch.inference_mode():
            scores = model.predict([(request["question"], passage) for passage in request["passages"]],
                                   batch_size=4, show_progress_bar=False)
        return {"ok": True, "scores": scores.tolist()}
    raise ValueError("Unsupported worker operation")


if __name__ == "__main__":
    try:
        request = json.loads(sys.stdin.read(4 * 1024 * 1024 + 1))
        print(json.dumps(run(request), allow_nan=False))
    except Exception:
        # Source text, model responses and service configuration never enter logs.
        print(json.dumps({"ok": False}))
        sys.exit(1)
