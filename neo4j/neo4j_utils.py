import os
from neo4j import GraphDatabase
from dotenv import load_dotenv

load_dotenv()

class Neo4jUtils:
    def __init__(self):
        uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        username = os.getenv("NEO4J_USERNAME", "neo4j")
        password = os.getenv("NEO4J_PASSWORD", "password")
        self.driver = GraphDatabase.driver(uri, auth=(username, password))

    def close(self):
        self.driver.close()

    def get_page_info(self, url):
        """Fetch the title and content of a specific page."""
        with self.driver.session() as session:
            result = session.run(
                "MATCH (p:Page {url: $url}) RETURN p.title AS title, p.content AS content",
                url=url
            )
            record = result.single()
            return record.data() if record else None

    def get_neighbors(self, url):
        """Fetch all pages linked to or semantically related to the given page."""
        with self.driver.session() as session:
            # We look for both LINKS_TO and SEMANTICALLY_RELATED edges
            query = """
            MATCH (p:Page {url: $url})-[r:LINKS_TO|SEMANTICALLY_RELATED]->(neighbor:Page)
            RETURN neighbor.url AS url, neighbor.title AS title, type(r) AS rel_type, r.text AS link_text
            """
            result = session.run(query, url=url)
            return [record.data() for record in result]

    def get_subgraph(self, seed_urls, depth=1):
        """Fetch a local subgraph around seed URLs up to a certain depth."""
        with self.driver.session() as session:
            query = """
            MATCH (p:Page)
            WHERE p.url IN $urls
            MATCH path = (p)-[*1..%d]-(neighbor:Page)
            RETURN path
            """ % depth
            result = session.run(query, urls=seed_urls)
            # In a real scenario, we'd process paths. For now, we'll return unique nodes and edges.
            nodes = set()
            edges = set()
            for record in result:
                path = record["path"]
                for node in path.nodes:
                    nodes.add((node.element_id, node.get("url"), node.get("title")))
                for rel in path.relationships:
                    edges.add((rel.start_node.element_id, rel.end_node.element_id, rel.type))

            return {"nodes": list(nodes), "edges": list(edges)}

if __name__ == "__main__":
    # Basic test
    utils = Neo4jUtils()
    print("Neo4jUtils initialized.")
    utils.close()
