"""Build the persistent Chroma collection from cleaned MetaKGP pages."""

import json
import logging
import os
import time

import chromadb
import torch
from sentence_transformers import SentenceTransformer

from RAG.text import chunk_text


logger = logging.getLogger(__name__)

INPUT_PATH = "Crawler/cleaned_wiki.jsonl"
VECTOR_STORE_PATH = "VectorStore"
COLLECTION_NAME = "metakgp_wiki"
EMBEDDING_MODEL = "BAAI/bge-large-en-v1.5"
INDEX_BATCH_SIZE = 512
EMBED_BATCH_SIZE = 64


def main() -> None:
    """Index pages not already represented in the persistent collection."""
    if not os.path.exists(INPUT_PATH):
        logger.error("Input file %s not found.", INPUT_PATH)
        return

    device = "cuda" if torch.cuda.is_available() else "cpu"
    logger.info("Loading %s on %s", EMBEDDING_MODEL, device.upper())
    model = SentenceTransformer(EMBEDDING_MODEL, device=device)

    logger.info("Opening ChromaDB at %s", VECTOR_STORE_PATH)
    client = chromadb.PersistentClient(path=VECTOR_STORE_PATH)
    collection = client.get_or_create_collection(name=COLLECTION_NAME)

    existing = collection.get(include=["metadatas"])
    existing_urls = {
        metadata["url"]
        for metadata in (existing.get("metadatas") or [])
        if metadata and metadata.get("url")
    }
    logger.info("Found %d already indexed pages.", len(existing_urls))

    batch_docs, batch_metadatas, batch_ids = [], [], []
    indexed_count = skipped_count = 0
    started_at = time.time()

    def persist_batch() -> int:
        """Encode and add the pending batch, returning its document count."""
        if not batch_docs:
            return 0
        logger.info("Encoding and adding %d chunks", len(batch_docs))
        embeddings = model.encode(
            batch_docs,
            batch_size=EMBED_BATCH_SIZE,
            show_progress_bar=False,
        ).tolist()
        collection.add(
            embeddings=embeddings,
            documents=batch_docs,
            metadatas=batch_metadatas,
            ids=batch_ids,
        )
        count = len(batch_docs)
        batch_docs.clear()
        batch_metadatas.clear()
        batch_ids.clear()
        return count

    with open(INPUT_PATH, "r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            try:
                page = json.loads(line)
                url = page["url"]
                if url in existing_urls:
                    skipped_count += 1
                    continue

                for chunk_index, chunk in enumerate(chunk_text(page.get("content", ""))):
                    batch_docs.append(chunk)
                    batch_metadatas.append(
                        {"url": url, "title": page.get("title", ""), "chunk_id": chunk_index}
                    )
                    batch_ids.append(f"{url}_{chunk_index}")

                if len(batch_docs) >= INDEX_BATCH_SIZE:
                    indexed_count += persist_batch()
            except (KeyError, json.JSONDecodeError) as exc:
                logger.warning("Skipping malformed source line %d: %s", line_number, exc)

    indexed_count += persist_batch()
    logger.info(
        "Indexing complete. Skipped %d pages; indexed %d chunks in %.2fs.",
        skipped_count,
        indexed_count,
        time.time() - started_at,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    main()
