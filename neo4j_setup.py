import json
import os
from neo4j import GraphDatabase

def main():
    # Neo4j Connection Details
    # User should set these as environment variables
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    username = os.getenv("NEO4J_USERNAME", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "password")

    try:
        driver = GraphDatabase.driver(uri, auth=(username, password))
    except Exception as e:
        print(f"Failed to connect to Neo4j: {e}")
        return

    input_path = 'Crawler/cleaned_wiki.jsonl'

    if not os.path.exists(input_path):
        print(f"Input file {input_path} not found.")
        return

    def setup_constraints(tx):
        # 1. Create Constraints for uniqueness
        tx.run("CREATE CONSTRAINT IF NOT EXISTS FOR (p:Page) REQUIRE p.url IS UNIQUE")
        tx.run("CREATE CONSTRAINT IF NOT EXISTS FOR (e:Entity) REQUIRE e.name IS UNIQUE")

    def create_graph(tx):
        print("Indexing data into Neo4j...")
        with open(input_path, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    data = json.loads(line)
                    url = data['url']
                    title = data['title']
                    links = data.get('linked_to', [])
                    content = data['content']

                    # Create Page node
                    tx.run(
                        "MERGE (p:Page {url: $url}) SET p.title = $title, p.content = $content",
                        url=url, title=title, content=content
                    )

                    # Create Links (Edges)
                    for link in links:
                        link_text = link['text']
                        link_url = link['url']

                        # Ensure the linked page exists
                        tx.run(
                            "MERGE (p2:Page {url: $url2})",
                            url2=link_url
                        )

                        # Create the relationship
                        tx.run(
                            "MATCH (p1:Page {url: $url1}), (p2:Page {url: $url2}) "
                            "MERGE (p1)-[:LINKS_TO {text: $text}]->(p2)",
                            url1=url, url2=link_url, text=link_text
                        )
                except Exception as e:
                    print(f"Error processing line: {e}")

    try:
        with driver.session() as session:
            session.execute_write(setup_constraints)
            session.execute_write(create_graph)
        print("Successfully imported data into Neo4j.")
    except Exception as e:
        print(f"Error during Neo4j import: {e}")
    finally:
        driver.close()

if __name__ == "__main__":
    main()
