import json
import os
from rapidfuzz import process, fuzz
from sentence_transformers import SentenceTransformer, util
from neo4j import GraphDatabase
from dotenv import load_dotenv

load_dotenv()

def main():
    # Neo4j Connection Details
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    username = os.getenv("NEO4J_USERNAME", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "password")

    try:
        driver = GraphDatabase.driver(uri, auth=(username, password))
    except Exception as e:
        print(f"Failed to connect to Neo4j: {e}")
        return

    # Load Embedding Model
    print("Loading embedding model for semantic similarity...")
    model = SentenceTransformer('BAAI/bge-large-en-v1.5')

    input_path = 'Crawler/cleaned_wiki.jsonl'

    if not os.path.exists(input_path):
        print(f"Input file {input_path} not found.")
        return

    def apply_mitigation(tx):
        # 1. Get all entities/pages from the graph
        result = tx.run("MATCH (p:Page) RETURN p.title AS title, p.url AS url")
        # Filter out pages with no title to avoid NoneType errors in embedding model
        pages = [{"title": record["title"], "url": record["url"]} for record in result if record["title"]]

        if not pages:
            print("No pages found in Neo4j graph.")
            return

        print(f"Applying mitigation to {len(pages)} pages...")

        # Precompute embeddings for all page titles
        titles = [p["title"] for p in pages]
        embeddings = model.encode(titles, convert_to_tensor=True)

        for i, page_i in enumerate(pages):
            # --- Strategy 1: Fuzzy String Matching (Entity Linking) ---
            # Find similar titles using RapidFuzz
            matches = process.extract(
                page_i["title"],
                titles,
                scorer=fuzz.WRatio,
                limit=5
            )

            for match_text, score, index in matches:
                if score > 90 and index != i:
                    page_j = pages[index]
                    tx.run(
                        "MATCH (p1:Page {url: $url1}), (p2:Page {url: $url2}) "
                        "MERGE (p1)-[:ENTITY_LINK {score: $score}]->(p2)",
                        url1=page_i["url"], url2=page_j["url"], score=score / 100.0
                    )

            # --- Strategy 2: Semantic Similarity (Soft Edges) ---
            # Find semantically similar pages using cosine similarity
            cos_sims = util.cos_sim(embeddings[i], embeddings)[0]

            # Get top 5 similar pages (excluding self)
            # We'll use a threshold (e.g., 0.75) to avoid adding too many noise edges
            for j, sim in enumerate(cos_sims):
                if i != j and sim > 0.75:
                    page_j = pages[j]
                    tx.run(
                        "MATCH (p1:Page {url: $url1}), (p2:Page {url: $url2}) "
                        "MERGE (p1)-[:SEMANTICALLY_RELATED {score: $score}]->(p2)",
                        url1=page_i["url"], url2=page_j["url"], score=float(sim)
                    )

    try:
        with driver.session() as session:
            session.execute_write(apply_mitigation)
        print("Sparse graph mitigation complete.")
    except Exception as e:
        print(f"Error during mitigation: {e}")
    finally:
        driver.close()

if __name__ == "__main__":
    main()
