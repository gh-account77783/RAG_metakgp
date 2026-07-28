import json
import logging
import os
import time
from rapidfuzz import process, fuzz
from sentence_transformers import SentenceTransformer, util
from neo4j import GraphDatabase
import torch
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "BAAI/bge-large-en-v1.5"
FUZZY_THRESHOLD = 93
SEMANTIC_THRESHOLD = 0.85
WRITE_BATCH_SIZE = 1000
PROGRESS_INTERVAL = 500

def main():
    # Neo4j Connection Details
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    username = os.getenv("NEO4J_USERNAME", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "password123")

    try:
        driver = GraphDatabase.driver(uri, auth=(username, password))
    except Exception as e:
        logger.error("Failed to connect to Neo4j: %s", e)
        return

    device = "cuda" if torch.cuda.is_available() else "cpu"
    logger.info("Loading %s on %s", EMBEDDING_MODEL, device.upper())
    model = SentenceTransformer(EMBEDDING_MODEL, device=device)

    input_path = 'Crawler/cleaned_wiki.jsonl'

    if not os.path.exists(input_path):
        logger.error("Input file %s not found.", input_path)
        return

    logger.info("Fetching pages from Neo4j")
    t_start = time.time()
    
    with driver.session() as session:
        result = session.run("MATCH (p:Page) RETURN p.title AS title, p.url AS url")
        pages = [{"title": record["title"], "url": record["url"]} for record in result if record["title"]]

    if not pages:
        logger.warning("No pages found in Neo4j graph.")
        driver.close()
        return

    logger.info("Found %d pages. Encoding titles for semantic similarity.", len(pages))
    titles = [p["title"] for p in pages]
    
    # GPU-accelerated embedding generation
    embeddings = model.encode(titles, convert_to_tensor=True, device=device)
    
    entity_links = []
    semantic_links = []

    logger.info("Computing fuzzy-string and cosine-similarity relationships")
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
            if score > FUZZY_THRESHOLD and index != i:
                page_j = pages[index]
                entity_links.append({
                    "url1": page_i["url"],
                    "url2": page_j["url"],
                    "score": float(score / 100.0)
                })

        # 2. Semantic similarity threshold (> 0.85)
        sim_scores = cos_sim_matrix[i]
        for j, sim in enumerate(sim_scores):
            if i != j and sim > SEMANTIC_THRESHOLD:
                page_j = pages[j]
                semantic_links.append({
                    "url1": page_i["url"],
                    "url2": page_j["url"],
                    "score": float(sim)
                })
                
        if (i + 1) % PROGRESS_INTERVAL == 0:
            logger.info("Computed similarities for %d/%d pages", i + 1, len(pages))

    logger.info("Computation complete in %.2fs.", time.time() - t_compute)
    logger.info("Found %d entity links and %d semantic links.", len(entity_links), len(semantic_links))

    # Write in batches using UNWIND
    batch_size = WRITE_BATCH_SIZE
    
    with driver.session() as session:
        # Write Entity Links
        if entity_links:
            logger.info("Writing %d ENTITY_LINK relationships to Neo4j", len(entity_links))
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
                logger.info("Wrote ENTITY_LINK batch %d-%d. Created: %d", start_idx, start_idx + len(batch), stats.get("relationships-created", 0))
                
        # Write Semantic Links
        if semantic_links:
            logger.info("Writing %d SEMANTICALLY_RELATED relationships to Neo4j", len(semantic_links))
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
                logger.info("Wrote SEMANTICALLY_RELATED batch %d-%d. Created: %d", start_idx, start_idx + len(batch), stats.get("relationships-created", 0))

    logger.info("Sparse graph mitigation complete in %.2fs.", time.time() - t_start)
    driver.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    main()
