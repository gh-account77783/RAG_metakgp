import json
import os
from sentence_transformers import SentenceTransformer
import chromadb
from chromadb.config import Settings

def semantic_chunking(text, chunk_size=512, overlap=50):
    """
    A simple chunking strategy that splits text into chunks of a fixed size
    with overlap to preserve context. For a more advanced approach,
    we could split by markdown headers.
    """
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

    # Initialize Embedding Model
    print("Loading embedding model: BAAI/bge-large-en-v1.5...")
    model = SentenceTransformer('BAAI/bge-large-en-v1.5')

    # Initialize ChromaDB
    print("Initializing ChromaDB...")
    client = chromadb.PersistentClient(path=db_path)
    collection = client.get_or_create_collection(name="metakgp_wiki")

    # Process and index documents
    print(f"Indexing documents from {input_path}...")
    with open(input_path, 'r', encoding='utf-8') as f:
        for i, line in enumerate(f):
            try:
                data = json.loads(line)
                url = data['url']
                title = data['title']
                content = data['content']

                # Chunk the content
                chunks = semantic_chunking(content)

                # Create metadata and documents for each chunk
                documents = []
                metadatas = []
                ids = []

                for j, chunk in enumerate(chunks):
                    documents.append(chunk)
                    metadatas.append({"url": url, "title": title, "chunk_id": j})
                    ids.append(f"{url}_{j}")

                # Generate embeddings
                embeddings = model.encode(documents).tolist()

                # Add to ChromaDB
                collection.add(
                    embeddings=embeddings,
                    documents=documents,
                    metadatas=metadatas,
                    ids=ids
                )

                if (i + 1) % 10 == 0:
                    print(f"Processed {i + 1} documents...")

            except Exception as e:
                print(f"Error processing document {i}: {e}")

    print(f"Indexing complete. Vector store saved at {db_path}.")

if __name__ == "__main__":
    main()
