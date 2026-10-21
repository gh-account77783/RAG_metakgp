"""Internal inference subprocess; no provider credentials, hub download or code loading."""
import json
from contextlib import redirect_stdout
from pathlib import Path
import sys

from .local_inference import verify_model


def run(request: dict) -> dict:
    if request["operation"] == "tree":
        from importlib.metadata import version
        from pageindex.flash import page_index_flash
        if version("pageindex") != "0.4.4":
            raise ValueError("PageIndex version differs from the index recipe")
        result = page_index_flash(Path(request["pdf_path"]), summary=False, optimize=False)
        return {"ok": True, "structure": result["structure"], "toc_source": result.get("toc_source")}
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
            scores = model.predict(request["pairs"],
                                   batch_size=4, show_progress_bar=False)
        return {"ok": True, "scores": scores.tolist()}
    raise ValueError("Unsupported worker operation")


if __name__ == "__main__":
    try:
        import socket
        original_connect = socket.socket.connect
        def offline_connect(connection, address):
            if connection.family in {socket.AF_INET, socket.AF_INET6}:
                raise OSError("Inference workers have no network access")
            return original_connect(connection, address)
        socket.socket.connect = offline_connect
        request = json.loads(sys.stdin.read(4 * 1024 * 1024 + 1))
        with redirect_stdout(sys.stderr):
            result = run(request)
        print(json.dumps(result, allow_nan=False))
    except Exception:
        # Source text, model responses and service configuration never enter logs.
        print(json.dumps({"ok": False}))
        sys.exit(1)
