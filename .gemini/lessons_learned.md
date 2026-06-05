# 🧠 Session History, Lessons Learned, & Tips (5th June 2026)

This document contains a comprehensive record of the debugging sessions, architectural patterns, and developer tips established during the refinement of the **GraphMind (RAG + GoT + MoE)** system on MetaKGP.

---

## 📋 1. Session History & Achievements

*   **Goal**: Diagnose and repair visualizer crashes, fix incomplete database traversals, optimize reasoning limits, and resolve query relevance gaps.
*   **Accomplishments**:
    *   Resolved `streamlit-agraph` API compatibility crash.
    *   Fixed absolute path resolution for relative vector database targets.
    *   Fixed truthy client bugs (`if not db` -> `if db is None`).
    *   Implemented structured URL extraction (`clean_lead_url`) from candidate string paths.
    *   Increased the traversal loop limit from 5 to 7.
    *   Pioneered and implemented the **Parallel Seed Ingestion** pattern to prevent missing relevant initial search results.
    *   Verified the complete pipeline using a complex biotechnology query.

---

## 🛠️ 2. Core Diagnostic Failures & Solutions

### Lesson A: The truthy object trap on databases in Python
*   **The Problem**: In `got_engine.py`, the code checked if the Chroma database client was loaded using:
    ```python
    if not db:
        raise ValueError("vector_store client not configured...")
    ```
    When the directory context changed, Chroma initialized a new, empty database. In python, collections that are empty evaluate as `False` (falsy) under boolean evaluation. This caused the system to falsely throw connection errors even though the client object was valid.
*   **The Lesson**: Always use specific identity checks (`is None`) for resource connection clients instead of truthy checks.
*   **The Fix**:
    ```python
    if db is None:
        raise ValueError(...)
    ```

### Lesson B: Candidate string pollution in DB queries
*   **The Problem**: The LLM was given candidates formatted as `"Title (URL)"` (e.g., `AG40007: Agricultural Biotechnology (https://wiki.metakgp.org/w/AG40007:_Agricultural_Biotechnology)`). The LLM picked this candidate as the `next_lead`. The code directly fed this unparsed string into Neo4j's search query `MATCH (p:Page {url: $url})`, failing to find any page because the URL was polluted with the title.
*   **The Lesson**: Never assume LLM choices are sanitized or clean. Sanitization boundaries should exist between LLM outputs and database query inputs.
*   **The Fix**: Created a regex extraction utility `clean_lead_url` to clean URL strings:
    ```python
    def clean_lead_url(lead: str) -> str:
        import re
        if not lead or lead == 'NONE':
            return 'NONE'
        match = re.search(r'\((https?://[^\)]+)\)', lead)
        if match:
            return match.group(1)
        match_any = re.search(r'https?://[^\s\)]+', lead)
        if match_any:
            return match_any.group(0)
        return lead.strip()
    ```

### Lesson C: UI library API version drifts
*   **The Problem**: Streamlit ran into `agraph() got an unexpected keyword argument 'device'`. This was caused by upgrading `streamlit-agraph` without updating the caller.
*   **The Lesson**: Keep rendering styling configuration separate from calling arguments. Use structured configuration classes where provided by UI packages.
*   **The Fix**: Bundled styling parameters inside a `Config` instantiation:
    ```python
    config = Config(width=800, height=300, directed=True, physics=True)
    agraph(nodes=nodes, edges=edges, config=config)
    ```

---

## 🏗️ 3. Architectural Pattern: Parallel Seed Ingestion

### The Relevance Gap in Sequential GoT
In sequential RAG graph search, the agent reads seed pages one by one. However:
1.  **Vector Search Limits**: A relevant page (e.g. the Biotech department page) might contain mostly administrative text, ranking lower in vector similarity (like rank #4) than academic courses containing biological keywords.
2.  **Disconnected Topology**: The highly-ranked academic courses might not link directly to the administrative department page in the graph database.

Under pure sequential search, the department page is never visited, leading to incomplete answers.

### The Solution (Parallel Ingestion)
Modify the entry node of the GoT graph (`seed_retrieval`) to ingest **all top 3 seeds** in parallel right at the start.
1.  **Read and Merge**: Load and concatenate the text of all 3 vector search results into the starting `knowledge_set` immediately.
2.  **Collect Neighbors**: Query Neo4j for the neighbors of **all 3 pages** and add them to the starting candidate list.
3.  **Traverse Deeply**: Use GoT graph traversal starting from this rich, multi-seed baseline.

This combination of standard multi-document RAG and GoT graph traversal ensures no initial search results are missed while maintaining advanced graph reasoning capabilities.

---

## 💡 4. General Tips & Tricks for RAG + Graph AI

1.  **Relative Path Warnings**: When running python scripts via Streamlit or daemon watchers, the current working directory (`Cwd`) may change. Always resolve relative database paths to absolute using `os.path.abspath(__file__)`.
2.  **Visited Page Sanitization**: When tracking visited pages to prevent loops, keep their representation uniform (e.g., standard clean URLs). If you mix title strings and raw URLs in your visited tracking set, the loop detection will fail.
3.  **Parallel Multi-Agent Judges (MoE)**: Running multiple expert prompt loops (Source Matcher, Logic Auditor, Hallucination Hunter) concurrently using `RunnableParallel` drastically improves auditing speed compared to sequential calls.
4.  **Transaction Consuming in Neo4j**: Always ensure Cypher queries consume results (`result.consume()`) or convert records directly to primitive values (`result.single()` or lists) before closing sessions to prevent silent rollbacks.
