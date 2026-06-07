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

---

# 🧠 Session History, Lessons Learned, & Tips (7th June 2026)

This section documents the security verification, bug fixes, FastMCP mount path configurations, and automated deployment scripts implemented for the remote serving of the **GraphMind MCP Server**.

## 📋 1. Session History & Achievements
*   **Goal**: Secure and productionize the remote MCP server (`mcp_server.py`), fix connection routing for Server-Sent Events (SSE), resolve potential runtime exception bugs, and create an automated deployment guide script.
*   **Accomplishments**:
    *   Fixed FastMCP SSE App Mount Path prefix routing by adding `mount_path="/mcp"` to `mcp.sse_app`.
    *   Resolved a potential crash (`IndexError`) in authentication middleware by replacing direct header splits with `split(maxsplit=1)`.
    *   Implemented EventSource-compatible query-string parameter authentication (`?token=...`) as a fallback for browser/web-based MCP clients.
    *   Improved Google public JWK caching resilience to handle API network timeouts by reusing cached certs.
    *   Configured `CORSMiddleware` on the FastAPI host to allow web-based preflight `OPTIONS` requests from remote clients.
    *   Caught and fixed a serialization bug where `neo4j.get_page_info` was returning custom Neo4j `Record` objects, converting them to standard python `dict` structures using `record.data()` and enforcing type safety on empty lookups.
    *   Staged all updated backend files and packaged all manual EC2 steps into an executable `deploy.sh` script.

---

## 🛠️ 2. Core Diagnostic Failures & Solutions

### Lesson A: FastMCP SSE Mount Path Trap
*   **The Problem**: Mounting the FastMCP sub-app inside FastAPI via `app.mount("/mcp", mcp.sse_app())` without passing `mount_path` defaults the internal SSE settings to the root path. When clients request the SSE channel, the server returns the message endpoint headers pointing to the root `/messages` instead of `/mcp/messages`.
*   **The Fix**: Explicitly declare `mount_path` during instantiation:
    ```python
    app.mount("/mcp", mcp.sse_app(mount_path="/mcp"))
    ```

### Lesson B: Web/Browser EventSource Custom Header Limits
*   **The Problem**: Traditional browser-based JavaScript `EventSource` (SSE client) APIs do not allow customizing HTTP headers (e.g., adding `Authorization: Bearer <token>`). As a result, web clients cannot connect to authenticated SSE endpoints.
*   **The Fix**: Add query string fallback check in the custom ASGI middleware:
    ```python
    if not token:
        import urllib.parse
        query_string = scope.get("query_string", b"").decode("utf-8")
        params = urllib.parse.parse_qs(query_string)
        token_list = params.get("token")
        if token_list:
            token = token_list[0]
    ```

### Lesson C: Custom Database Objects Serialization Crashes
*   **The Problem**: Neo4j session queries return driver-specific `Record` objects. While they support dictionary-style access (`record['key']`), they are custom Python classes that will raise a serialization error when FastMCP attempts to return them in JSON format to the MCP client.
*   **The Fix**: Always call `record.data()` to serialize the query outcomes to standard Python primitives at the database driver boundary.

---

## 💡 3. General Tips & Tricks for Remote MCP Deployment
1.  **Header Splitting Safety**: When parsing headers manually in ASGI middleware, never assume index positions (like `split(" ")[1]`). Always check parts length or use `split(maxsplit=1)` to prevent `IndexError` crashes.
2.  **CORS for Headless Tools**: Even if MCP servers are primarily designed for desktop apps (Claude Desktop), they are increasingly consumed by browser-based IDEs (Cursor, IDX) and dashboard UIs. Register `CORSMiddleware` early.
3.  **Local Sync vs Async Event Loops**: When running intensive calculations (like GoT LangGraph chains or complex SQL queries) inside async FastAPI handlers, wrap the calls in `asyncio.to_thread` to keep the event loop unblocked.

---

# 🧠 Session History, Lessons Learned, & Tips (8th June 2026)

This section documents the debugging, Nginx reverse-proxy configuration adjustments, python-jose OIDC validation fixes, routing resolution, and Neo4j database restoration steps implemented during the local and remote deployment of the **GraphMind MCP Server**.

## 📋 1. Session History & Achievements
*   **Goal**: Establish a working connection between local Claude Code CLI and remote AWS EC2 MCP Server, resolve JWT decoding validation failures, bypass host-header DNS rebinding blocks, and troubleshoot graph search hopping failures.
*   **Achievements**:
    *   Resolved `at_hash` validation error by disabling the access token hash checks in `jwt.decode`.
    *   Bypassed the MCP SDK's built-in DNS Rebinding protection check by configuring Nginx to forward `Host 127.0.0.1:8000` to the Uvicorn backend.
    *   Fixed the client-side `HTTP 404: Not Found` double-prefix routing issue on POST requests by removing the explicit `mount_path` from FastMCP's `sse_app`.
    *   Successfully established the remote connection between Claude Code CLI and GraphMind MCP Server.
    *   Diagnosed GoT reasoning "hopping" failure where queries returned empty candidates due to an empty remote Neo4j instance.
    *   Formulated a low-resource database transfer plan (compressing the 21.8 MB local database folder and uploading it via `scp`) to bypass heavy server-side schema reconstruction.
    *   Documented the exact extraction, container mount check, and Docker container recreate steps for the next session.
    *   Documented a comprehensive, step-by-step [AWS_DEPLOYMENT.md](file:///D:/programming/RAG_and_MCP_examples/AWS_DEPLOYMENT.md) guide.

---

## 🛠️ 2. Core Diagnostic Failures & Solutions

### Lesson A: Pip RAM-based `/tmp` disk quota constraint
*   **The Problem**: Running `pip install` on heavy dependencies (PyTorch + CUDA wheels, totaling 3+ GB) on standard EC2 instances can trigger `OSError: [Errno 122] Disk quota exceeded`. This is because pip defaults to unpacking downloads in `/tmp`, which is mounted as a RAM-based `tmpfs` disk capped at 1.9 GB, even if the primary EBS root disk has 20+ GB free.
*   **The Fix**: Override the temporary directory environment variable (`TMPDIR`) to a folder on the persistent EBS disk, and run pip with `--no-cache-dir`:
    ```bash
    TMPDIR=/home/ubuntu/RAG_and_MCP_examples/tmp pip install --no-cache-dir -r requirements.txt
    ```

### Lesson B: python-jose `at_hash` token validation crash
*   **The Problem**: Google ID Tokens contain an `at_hash` (Access Token Hash) claim. When using `python-jose`'s `jwt.decode` to validate OIDC identity, the library fails with `JWTError: No access_token provided to compare against at_hash claim` because the MCP client only sends the ID Token.
*   **The Fix**: Pass `options={"verify_at_hash": False}` to the `jwt.decode` call to skip the access token hash check, which is safe since the ID token's signature is already validated using Google's public JWK certs:
    ```python
    jwt.decode(token, key, audience=client_id, options={"verify_at_hash": False})
    ```

### Lesson C: MCP SDK DNS Rebinding Protection / Host Header Mismatch
*   **The Problem**: The MCP Python SDK's `SseServerTransport` incorporates `TransportSecurityMiddleware` to prevent DNS rebinding. It checks the request's `Host` header. Under Nginx reverse proxy settings, Nginx passes `Host $host` (e.g. `ragmcp.duckdns.org`), which the SDK rejects with `ValueError: Request validation failed` and returns `421 Misdirected Request` (`Invalid Host header`). Setting the host to `localhost` or `127.0.0.1` without the port still fails because Uvicorn is bound to `127.0.0.1:8000` and the port mismatch triggers the validation failure.
*   **The Fix**: Configure Nginx's reverse proxy block to explicitly pass the exact loopback IP address and port that the Uvicorn process is listening on:
    ```nginx
    location /mcp {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host 127.0.0.1:8000;
        ...
    }
    ```

### Lesson D: FastMCP Double Prefix 404 (`/mcp/mcp/messages/`)
*   **The Problem**: When mounting the FastMCP SSE sub-app inside a main FastAPI app via `app.mount("/mcp", mcp.sse_app())`, FastAPI passes the mount prefix `/mcp` in the ASGI scope as `root_path`. If `mount_path="/mcp"` is also explicitly passed to `sse_app(mount_path="/mcp")`, the FastMCP SDK appends the prefix twice, advertising the message endpoint as `/mcp/mcp/messages/?session_id=...` which returns a `404 Not Found` error when the client attempts to POST to it.
*   **The Fix**: Mount the sub-app without the explicit `mount_path` parameter, allowing the ASGI `root_path` propagation to handle the prefix cleanly:
    ```python
    app.mount("/mcp", mcp.sse_app())
    ```

### Lesson E: Graph Search Hopping Failure due to Empty Neo4j Database
*   **The Problem**: After setting up a clean Neo4j instance on EC2, the database is empty by default and lacks any nodes or relationships (causing the warning: `warn: label does not exist. The label Page does not exist in database neo4j`). Consequently, the GoT engine queries for linked page candidates return zero results, preventing the LLM from making any "hops" or traversals.
*   **The Fix**: Instead of running resource-heavy import scripts on a small remote instance, migrate the database from the local environment:
    1.  Zip the local Neo4j Desktop database `data` folder (excluding application settings and binaries).
    2.  `scp` the zip to the EC2 host.
    3.  Stop the remote Neo4j Docker container, clear old data, and extract the archive directly to `neo4j/data/` (ensuring no double-nested directory issue).
    4.  Apply docker-friendly owner permissions `sudo chown -R 7474:7474 neo4j/data` and restart the container to restore the graph state immediately.
    5.  Verify mounts via `sudo docker inspect -f '{{ .Mounts }}' neo4j` to ensure the host path matches the container destination.
