import os
import json
import time
from sentence_transformers import SentenceTransformer
import chromadb
import torch

def semantic_chunking(text, chunk_size=512, overlap=50):
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        chunks.append(chunk)
        start += chunk_size - overlap
    return chunks

def main():
    input_path = 'Crawler/cleaned_wiki.jsonl'
    db_path = 'VectorStore'

    if not os.path.exists(input_path):
        print(f"Input file {input_path} not found.")
        return

    # Initialize Embedding Model with GPU/CPU auto-detection
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Loading embedding model: BAAI/bge-large-en-v1.5 on {device.upper()}...")
    model = SentenceTransformer('BAAI/bge-large-en-v1.5', device=device)

    # Initialize ChromaDB
    print("Initializing ChromaDB...")
    client = chromadb.PersistentClient(path=db_path)
    collection = client.get_or_create_collection(name="metakgp_wiki")

    # Get already indexed URLs to skip them (idempotency)
    print("Fetching existing documents from ChromaDB...")
    existing = collection.get(include=['metadatas'])
    existing_urls = set()
    if existing and 'metadatas' in existing:
        for m in existing['metadatas']:
            if m and 'url' in m:
                existing_urls.add(m['url'])
    print(f"Found {len(existing_urls)} already indexed pages in ChromaDB.")

    # Process and index documents in batches
    print("Scanning wikitext dataset...")
    batch_docs = []
    batch_metadatas = []
    batch_ids = []
    
    indexed_count = 0
    skipped_count = 0
    t_start = time.time()
    
    with open(input_path, 'r', encoding='utf-8') as f:
        for i, line in enumerate(f):
            try:
                data = json.loads(line)
                url = data['url']
                title = data['title']
                content = data['content']

                if url in existing_urls:
                    skipped_count += 1
                    continue

                chunks = semantic_chunking(content)
                for j, chunk in enumerate(chunks):
                    batch_docs.append(chunk)
                    batch_metadatas.append({"url": url, "title": title, "chunk_id": j})
                    batch_ids.append(f"{url}_{j}")

                # Using larger batch sizes for high GPU utilization
                if len(batch_docs) >= 512:
                    print(f"Encoding and adding batch of {len(batch_docs)} chunks on GPU...")
                    t0 = time.time()
                    embeddings = model.encode(batch_docs, batch_size=64, show_progress_bar=False).tolist()
                    collection.add(
                        embeddings=embeddings,
                        documents=batch_docs,
                        metadatas=batch_metadatas,
                        ids=batch_ids
                    )
                    indexed_count += len(batch_docs)
                    duration = time.time() - t0
                    print(f"Batch indexed in {duration:.2f}s ({len(batch_docs)/duration:.1f} chunks/sec). Total indexed: {indexed_count}")
                    batch_docs = []
                    batch_metadatas = []
                    batch_ids = []

            except Exception as e:
                print(f"Error processing line {i}: {e}")

    # Process remaining chunks in the last batch
    if batch_docs:
        print(f"Encoding and adding final batch of {len(batch_docs)} chunks on GPU...")
        embeddings = model.encode(batch_docs, batch_size=64, show_progress_bar=False).tolist()
        collection.add(
            embeddings=embeddings,
            documents=batch_docs,
            metadatas=batch_metadatas,
            ids=batch_ids
        )
        indexed_count += len(batch_docs)
        
    print(f"Indexing complete! Skipped {skipped_count} pages. Newly indexed {indexed_count} chunks.")
    print(f"Total time elapsed: {time.time() - t_start:.2f}s")

if __name__ == "__main__":
    main()
