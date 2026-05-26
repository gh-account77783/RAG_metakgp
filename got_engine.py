import chromadb
from sentence_transformers import SentenceTransformer
from neo4j_utils import Neo4jUtils
from llm_client import LLMClient
import networkx as nx

class GoTReasoningEngine:
    def __init__(self, vector_store_path='VectorStore'):
        # Initialize Vector Store
        self.chroma_client = chromadb.PersistentClient(path=vector_store_path)
        self.collection = self.chroma_client.get_or_create_collection(name="metakgp_wiki")
        self.embedding_model = SentenceTransformer('BAAI/bge-large-en-v1.5')

        # Initialize Graph Utils and LLM
        self.neo4j = Neo4jUtils()
        self.llm = LLMClient()

    def _get_seed_pages(self, query, k=3):
        """Retrieve the top-k relevant chunks from ChromaDB to find entry pages."""
        query_embedding = self.embedding_model.encode([query]).tolist()
        results = self.collection.query(
            query_embeddings=query_embedding,
            n_results=k
        )

        seed_urls = set()
        for meta in results['metadatas'][0]:
            seed_urls.add(meta['url'])

        return list(seed_urls)

    def _analyze_and_decide(self, query, knowledge_set, candidates):
        """
        LLM decides if the query is answered or which candidate page to explore next.
        """
        system_prompt = (
            "You are a reasoning agent. Your goal is to answer a query based ONLY on provided knowledge. "
            "You have a 'Knowledge Set' of facts and a list of 'Candidate Pages' you can explore in the graph. "
            "Decide if you have enough information to answer. If not, pick the most promising candidate page to explore."
        )

        prompt = f"""
        Query: {query}

        Current Knowledge Set:
        {knowledge_set}

        Candidate Pages for Exploration:
        {candidates}

        Respond in the following format:
        ANSWER: <the final answer if you have enough info, else 'NONE'>
        NEXT_LEAD: <the URL of the page to explore next, or 'NONE'>
        REASONING: <brief explanation of why this page is the next best lead>
        """

        response = self.llm.generate(prompt, system_prompt=system_prompt)
        return response

    def reason(self, query, max_iterations=5):
        """Main GoT loop: Retrieval -> Graph Expansion -> Iterative Reasoning."""
        knowledge_set = ""
        visited_pages = set()
        thought_graph = nx.DiGraph()

        # 1. Initial Seed Retrieval
        print(f"Searching for seed pages for: {query}...")
        seed_urls = self._get_seed_pages(query)

        # Add seed pages to candidates
        candidates = []
        for url in seed_urls:
            info = self.neo4j.get_page_info(url)
            if info:
                candidates.append(f"{info['title']} ({url})")

        # 2. Iterative Reasoning Loop
        for i in range(max_iterations):
            print(f"Iteration {i+1}/{max_iterations}...")

            # LLM Analysis
            decision = self._analyze_and_decide(query, knowledge_set, candidates)

            # Check if answer is found
            if "ANSWER:" in decision and "NONE" not in decision.split("ANSWER:")[1].split("\n")[0]:
                answer = decision.split("ANSWER:")[1].split("\n")[0].strip()
                print("Answer found!")
                return {
                    "answer": answer,
                    "path": list(thought_graph.nodes),
                    "knowledge": knowledge_set
                }

            # Pick next lead
            next_lead_line = [line for line in decision.split("\n") if line.startswith("NEXT_LEAD:")][0]
            next_url = next_lead_line.replace("NEXT_LEAD:", "").strip().strip('()')

            if not next_url or next_url == "NONE":
                print("No more promising leads. Synthesizing answer from current knowledge...")
                break

            # Extract URL from the candidate string if it was passed as "Title (URL)"
            if "(" in next_url and ")" in next_url:
                next_url = next_url[next_url.find("(")+1:next_url.find(")")]

            # Explore the lead
            print(f"Exploring lead: {next_url}...")
            info = self.neo4j.get_page_info(next_url)
            if info:
                content = info['content']
                knowledge_set += f"\n--- Page: {info['title']} ({next_url}) ---\n{content}\n"
                visited_pages.add(next_url)

                # Update Thought Graph
                thought_graph.add_node(next_url, title=info['title'])

                # Expand candidates from neighbors
                neighbors = self.neo4j.get_neighbors(next_url)
                for n in neighbors:
                    if n['url'] not in visited_pages:
                        candidates.append(f"{n['title']} ({n['url']})")

            # Remove current lead from candidates
            candidates = [c for c in candidates if next_url not in c]

        # Final synthesis if loop finishes without a direct answer
        print("Synthesizing final answer...")
        final_prompt = f"Query: {query}\n\nKnowledge Set:\n{knowledge_set}\n\nProvide a final answer based strictly on the knowledge set. If the answer is not there, say 'I don't know'."
        answer = self.llm.generate(final_prompt)

        return {
            "answer": answer,
            "path": list(thought_graph.nodes),
            "knowledge": knowledge_set
        }

    def close(self):
        self.neo4j.close()

if __name__ == "__main__":
    # Simple test run
    engine = GoTReasoningEngine()
    try:
        res = engine.reason("Who are the governors of the Technology Literary Society?")
        print("\nFinal Answer:\n", res['answer'])
        print("\nThought Path:\n", res['path'])
    finally:
        engine.close()
