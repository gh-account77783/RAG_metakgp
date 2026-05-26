# GraphMind Project Workflow Plan (Post-Crawler)

This document outlines the implementation strategy for the "GraphMind" conversational AI after the data scraping phase is complete.

## Phase 1: Data Ingestion & Knowledge Base Setup
**Goal:** Transform raw scraped data into a searchable and linked knowledge base.
- [ ] **Data Cleaning & Semantic Refining:**
    - *Noise Removal:* Strip "page does not exist" labels and redundant markdown artifacts (e.g., `Unnamed: 6`).
    - *Table Processing:* Convert Markdown tables into structured natural language sentences or direct triplets for the Knowledge Graph.
    - *Link Normalization:* Standardize all internal wiki links to a consistent format for easier entity linking.
- [ ] **Chunking Strategy:** Develop a semantic chunking mechanism to preserve context (e.g., splitting by section or fixed-size with overlap).
- [ ] **Embedding Generation:** Select and integrate an embedding model (e.g., `BAAI/bge-large-en-v1.5`).
- [ ] **Vector Store Implementation:** Set up a vector database (e.g., ChromaDB) to store and retrieve document chunks.
- [ ] **Neo4j Database Setup:** Initialize Neo4j instance and define schema for entities (Nodes) and relationships (Edges).
- [ ] **Sparse Graph Mitigation:**
    - **Entity Linking (Hybrid Pipeline):**
        - *Candidate Generation:* Use RapidFuzz for string matching and SBERT (bi-encoders) for semantic candidate retrieval.
        - *Disambiguation:* Use Cross-Encoders to rank candidates and resolve mentions to the correct entity.
    - **Semantic Similarity:** Create "soft edges" between nodes that are semantically similar even without direct wiki links, using embedding cosine similarity.

## Phase 2: Graph of Thoughts (GoT) Implementation
**Goal:** Move beyond simple RAG by modeling information as a reasoning graph.
- [ ] **Knowledge Graph Extraction:**
    - Extract entities (Societies, Students, Events) and relationships from the cleaned text.
    - Map internal wiki links as edges between nodes.
- [ ] **Graph Construction:** Implement the graph using `NetworkX` for reasoning and coordinate with the Neo4j backend.
- [ ] **Reasoning Engine:**
    - Implement GoT traversal: instead of one retrieval, the system should explore "paths" of thought.
    - Example: If searching for a person, find their society $\rightarrow$ find society governors $\rightarrow$ verify current year.

## Phase 3: Mixture of Experts (MoE) Verification
**Goal:** Ensure 100% fidelity to the scraped data and eliminate hallucinations.
- [ ] **Expert 1: Source Matcher:** Implement a verifier that checks if the generated claim is explicitly supported by the retrieved text chunks.
- [ ] **Expert 2: Hallucination Hunter:** Implement a cross-check mechanism to identify any information in the answer that does *not* appear in the provided context.
- [ ] **Expert 3: Logic Expert:** Implement a check to ensure the final conclusion follows logically from the premises extracted in the GoT phase.
- [ ] **Verification Orchestrator:** Create a "judge" model/prompt that aggregates expert scores to either accept the answer or trigger a re-generation.

## Phase 4: Chatbot Integration & UI
**Goal:** Provide a user-facing interface that demonstrates the reasoning process.
- [ ] **Strict Context Prompting:** Engineer system prompts that forbid the use of pre-trained knowledge (forcing "I don't know" responses).
- [ ] **Integration Pipeline:** Connect the flow: `User Query` $\rightarrow$ `RAG Retrieval` $\rightarrow$ `GoT Expansion` $\rightarrow$ `MoE Verification` $\rightarrow$ `Final Answer`.
- [ ] **Frontend Development:** Build a Streamlit app featuring:
    - Chat interface.
    - Source citations with direct links to MetaKGP.
    - Visual representation of the Graph of Thoughts used for the query.

## Phase 5: Evaluation & Refinement
**Goal:** Stress-test the system against the "Trust, but Verify" requirements.
- [ ] **Gold Dataset Creation:** Manually curate a set of complex questions and verified answers from MetaKGP.
- [ ] **Fidelity Testing:** Measure the rate of hallucinations and "I don't know" correctness.
- [ ] **Performance Tuning:** Optimize retrieval speed and reasoning latency.