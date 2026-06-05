import json
import os
import time
from rapidfuzz import process, fuzz
from sentence_transformers import SentenceTransformer, util
from neo4j import GraphDatabase
from dotenv import load_dotenv

load_dotenv()

def main():
    # Neo4j Connection Details
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    username = os.getenv("NEO4J_USERNAME", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "password123")

    try:
        driver = GraphDatabase.driver(uri, auth=(username, password))
    except Exception as e:
        print(f"Failed to connect to Neo4j: {e}")
        return

    # Load Embedding Model on GPU
    print("Loading embedding model for semantic similarity on GPU (RTX 4050)...")
    model = SentenceTransformer('BAAI/bge-large-en-v1.5', device='cuda')

    input_path = 'Crawler/cleaned_wiki.jsonl'

    if not os.path.exists(input_path):
        print(f"Input file {input_path} not found.")
        return

    print("Fetching pages from Neo4j...")
    t_start = time.time()
    
    with driver.session() as session:
        result = session.run("MATCH (p:Page) RETURN p.title AS title, p.url AS url")
        pages = [{"title": record["title"], "url": record["url"]} for record in result if record["title"]]

    if not pages:
        print("No pages found in Neo4j graph.")
        driver.close()
        return

    print(f"Found {len(pages)} pages. Encoding titles to compute similarity matrix on GPU...")
    titles = [p["title"] for p in pages]
    
    # GPU-accelerated embedding generation
    embeddings = model.encode(titles, convert_to_tensor=True, device='cuda')
    
    entity_links = []
    semantic_links = []

    print("Computing relationships (Fuzzy String & Cosine Similarity)...")
    t_compute = time.time()
    
    # Compute similarity matrix on GPU
    cos_sim_matrix = util.cos_sim(embeddings, embeddings)
    
    for i, page_i in enumerate(pages):
        # 1. Fuzzy string matching
        matches = process.extract(
            page_i["title"],
            titles,
            scorer=fuzz.WRatio,
            limit=5
        )
        for match_text, score, index in matches:
            if score > 93 and index != i:
                page_j = pages[index]
                entity_links.append({
                    "url1": page_i["url"],
                    "url2": page_j["url"],
                    "score": float(score / 100.0)
                })

        # 2. Semantic similarity threshold (> 0.85)
        sim_scores = cos_sim_matrix[i]
        for j, sim in enumerate(sim_scores):
            if i != j and sim > 0.85:
                page_j = pages[j]
                semantic_links.append({
                    "url1": page_i["url"],
                    "url2": page_j["url"],
                    "score": float(sim)
                })
                
        if (i + 1) % 500 == 0:
            print(f"Computed similarities for {i+1}/{len(pages)} pages...")

    print(f"Computation complete in {time.time() - t_compute:.2f}s.")
    print(f"Found {len(entity_links)} entity links and {len(semantic_links)} semantic links.")

    # Write in batches using UNWIND
    batch_size = 1000
    
    with driver.session() as session:
        # Write Entity Links
        if entity_links:
            print(f"Writing {len(entity_links)} ENTITY_LINK relationships to Neo4j...")
            for start_idx in range(0, len(entity_links), batch_size):
                batch = entity_links[start_idx:start_idx + batch_size]
                query = """
                UNWIND $batch AS link
                MATCH (p1:Page {url: link.url1}), (p2:Page {url: link.url2})
                MERGE (p1)-[r:ENTITY_LINK]->(p2)
                SET r.score = link.score
                """
                res = session.run(query, batch=batch)
                stats = res.consume().metadata.get("stats", {})
                print(f"Wrote ENTITY_LINK batch {start_idx} to {start_idx + len(batch)}. Created: {stats.get('relationships-created', 0)}")
                
        # Write Semantic Links
        if semantic_links:
            print(f"Writing {len(semantic_links)} SEMANTICALLY_RELATED relationships to Neo4j...")
            for start_idx in range(0, len(semantic_links), batch_size):
                batch = semantic_links[start_idx:start_idx + batch_size]
                query = """
                UNWIND $batch AS link
                MATCH (p1:Page {url: link.url1}), (p2:Page {url: link.url2})
                MERGE (p1)-[r:SEMANTICALLY_RELATED]->(p2)
                SET r.score = link.score
                """
                res = session.run(query, batch=batch)
                stats = res.consume().metadata.get("stats", {})
                print(f"Wrote SEMANTICALLY_RELATED batch {start_idx} to {start_idx + len(batch)}. Created: {stats.get('relationships-created', 0)}")

    print(f"Sparse graph mitigation complete in {time.time() - t_start:.2f}s!")
    driver.close()


if __name__ == "__main__":
    main()

