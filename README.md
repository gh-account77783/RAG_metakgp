# GraphMind

GraphMind is a self-hosted document RAG service backed by Chroma and a Neo4j document-reference graph. The installable package provides five-format ingestion, local/hosted answers, authenticated browser reading and remote MCP. One Keycloak authority, active-account checks and a shared reader service protect both interfaces.

## Current validation boundary

As of 2026-10-21, the agreed P5/P6 functional end-to-end checkpoint and bounded
native identity-component feasibility on Windows 11 and Windows Server 2025
are complete. Recorded tests cover two readers, local/hosted modes, browser,
Claude Code and an independent MCP SDK client. The later structural refactor
`7216c21` passed [Windows/Ubuntu offline and outside-checkout package CI](https://github.com/gh-account77783/RAG_metakgp/actions/runs/39586921046)
plus secret scanning; its source and installed-wheel suites passed 149 tests
with no skips. That refactor added no new live-model or native-service acceptance.
Optional real-stores CI was skipped on push, not counted as acceptance.

These are bounded development results, not production/release qualification.
The last full-corpus five-question answer-quality smoke passed only **1/5**;
full frozen evaluation and manual factual/citation review remain open. Guided
installation, full native application services/reboot, resource qualification,
backup/restore, consolidated security/edge cases and release rights remain
P7/P8. Windows feasibility did not qualify the complete model/RAG stack.
The current CLI/MCP catalogue implements GraphMind and bounded PDF tree
navigation. The browser form currently uses GraphMind. The visual namespace
remains unavailable.

The original MetaKGP crawler and reasoning code remains in the repository as a legacy test prototype. It is not part of the generic package path and is not intended for the final user distribution.

## PDF tree navigation

Install the `tree` extra with the checked-in dependency constraints, or use a
qualified [offline bundle](documentation/RELEASE_BUNDLES.md). Set
`enabled_engines = ["tree_navigation"]` in the host configuration for vectorless
PDF use; this mode needs the configured answer provider and no embedding or
Neo4j service. Both engines can be enabled for a shared source.

```text
graphmind --config reader.toml import manual.pdf --engines tree_navigation
graphmind --config reader.toml status
graphmind --config reader.toml query "What is the maintenance interval?" --engine tree_navigation --document DOCUMENT_ID
```

Select one PDF per question. Indexing extracts its hierarchy locally without
model-written summaries; bounded navigation and generation use the configured
answer model and original pages. Citations identify the exact active version
and page. Text-bearing PDFs are supported; OCR is excluded. Failed replacement
builds preserve the previous source and ready indexes. Status reports each
engine's state, and selected engines share one source/original and durable job.

## Generic document ingestion milestone

Python 3.13, a running Neo4j instance, and the values in `config/.env.example` are the current tested baseline. Copy `config/graphmind.example.toml` to a private host configuration and adjust its data and import paths, then run:

```powershell
python -m pip install .
graphmind --config config/graphmind.example.toml init
graphmind --config config/graphmind.example.toml doctor
graphmind --config config/graphmind.example.toml import .\document.txt
graphmind --config config/graphmind.example.toml import .\manual.pdf
graphmind --config config/graphmind.example.toml reindex .\document.txt
graphmind --config config/graphmind.example.toml status
graphmind --config config/graphmind.example.toml query "What does the document say?"
graphmind --config config/graphmind.example.toml delete <document-id>
```

Text extraction runs in a time-limited subprocess. PDF OCR and password-protected files are rejected explicitly; DOCX macros, embedded objects, and external relationships are never processed; Markdown HTML and CSV formulas remain inert text. Import roots, source size, extracted text, archive expansion, archive entries, PDF pages, parser time, queue size, and chunk size are configurable host limits.

Deletion removes a document from the active manifest before its durable cleanup job touches Chroma, Neo4j, or private originals. A failed cleanup remains retryable with `graphmind retry <job-id>`. Reindexing creates a new immutable version when parser, chunker, embedding configuration, or document content changes, and the previous version remains active until both retrieval stores verify the replacement.

Immediate `import`, `reindex` and `delete` commands execute their own submitted
job through the shared durable executor. They leave unrelated queued jobs for
the worker and return success only after their job succeeds. JSON output keeps
the installation status and adds `submitted_job` with its ID, state, progress
and error code. A failure or busy writer returns a nonzero exit status; the
recorded job remains available for inspection/retry. `--enqueue-only` reports
the actual state without running work. `graphmind retry <job-id>` queues a failed
job; `graphmind worker-once` processes at most one eligible queued job in FIFO
order. Unknown job IDs are errors. Evaluation imports reuse the same targeted
executor.

A targeted job cannot overtake an older queued/running job for the same
document. It returns `job_not_ready` until that earlier work is completed, so a
later worker cannot republish an older pending version after the replacement.

For host-local folder ingestion and an interactive operator menu:

```bash
graphmind --config reader.toml import-folder /approved/documents --recursive --max-files 100
graphmind --config reader.toml wizard
```

Folder import selects supported files inside the configured roots, skips links,
hidden subdirectories and application runtime data, and checks the file limit
before submitting any work. Each file reports its actual job and engine-index
states; a failed file keeps its error and does not hide other file results.
Use `--engines tree_navigation` for an explicit compatible selection, or omit it
to use configured engines compatible with each format. `--enqueue-only` leaves
execution to the existing worker. Importing a changed file through the same path
uses the existing replacement/version rules. The menu reuses configuration,
dependency checks, import, status, failed-job retry and document deletion.

When a reader and host CLI run concurrently, configure the separate loopback
Chroma server as described in [ARCHITECTURE.md](documentation/ARCHITECTURE.md#chroma-persistence-ownership).
These host commands do not expose an upload or administration route to readers.

Run `graphmind --config reader.toml worker --poll-seconds 5` to process queued
ingestion continuously through the existing single-writer executor. SIGINT or
SIGTERM stops after the current job; failed jobs retain their status and require
an explicit retry. The worker reconciles interrupted jobs and backs off while
idle or when dependencies fail. Configure a service stop timeout long enough
for the bounded parser/model job to finish. Use the same private data directory
and loopback Chroma server as the reader; the worker needs host import access.

`min_free_disk_bytes` (environment: `GRAPHMIND_MIN_FREE_DISK_BYTES`) keeps a
512 MiB default reserve on the local application data volume. Import admission
and index jobs check it; low space reports `storage_capacity_unavailable` and
leaves the previous active source available. Free space and retry the failed
job. Deletion remains available for cleanup. Readiness reports low capacity;
this preflight check is not a quota, and separate database/model volumes need
their own capacity monitoring.

The deterministic hash embedding remains an offline diagnostic adapter only. The production candidate uses `BAAI/bge-m3` through same-host Ollama and fingerprints its full digest, reported precision and 1024-dimensional output. Changing that identity requires explicit reindexing. Bounded real identity/client functionality is verified; full quality, installers and release qualification remain later gates.

The local `gemma4:e2b` and `gemma4:e4b` presets request `think: false` for their bounded
structured answer: otherwise reasoning can exhaust the output-token limit before
any final JSON arrives. Reasoning traces are not treated as answers or citations.
Other model and hosted thinking controls are unchanged. Configure CPU request
timeouts explicitly; total provider attempts must fit inside the proxy's read
budget. These settings and green contract tests do not establish model quality.

## P4 model and retrieval evaluation

`graphmind evaluate` materializes the frozen 50-case MetaKGP fixture into 49
ordinary Markdown documents, imports them through the production pipeline, and
writes per-case retrieval, graph, answer, citation, provider-call, and latency
results. It does not treat the development JSONL snapshot as a supported user
format. Each case retains the selected original citation passages and locators
for manual support review. Review factual support and additional claims even
when the automatic URL-coverage score passes.

With the configured model/store services already available, run the evaluation
CLI against an isolated data root:

```bash
graphmind --config reader.toml evaluate \
  --fixture tests/fixtures/evaluation/metakgp_50.json \
  --source legacy/Crawler/cleaned_wiki.jsonl \
  --work-dir .evaluation/local --report .evaluation/local-report.json --label local
```

The historical shell wrapper `tools/evaluation/p4-evaluate.sh` pins Ollama 0.34.0,
verifies local `bge-m3` and `gemma4:e2b`, records
their full digests, runs the 50-case local chunk-scope baseline, then compares
vector-only and document-scope retrieval. Add `--with-hosted` after configuring
the private dotenv reported by `deploy/legacy/test-deploy.sh`, then use `--hosted-only` to run
the hosted-answer baseline without loading local `gemma4:e2b`; it still uses
local `bge-m3` so the embedding/retrieval configuration stays identical. `--with-hosted` runs
both modes in one self-contained session. Reports and host evidence remain outside
the checkout under `~/.local/state/graphmind-p4-evaluation`.

Native Windows uses the same evaluation contract:

```powershell
.\tools/evaluation/p4-evaluate.ps1 -PullModels
.\tools/evaluation/p4-evaluate.ps1 -HostedOnly
```

Both scripts require a clean committed checkout and preserve failed quality
reports before returning exit code 4 when a selected accepted baseline misses
its threshold. They do not expose a service, change a security group, or implement the
later browser/OAuth/MCP phases.

## Ubuntu 24.04 EC2 validation

`deploy/legacy/test-deploy.sh` retains the historical P1-P3 test harness for a clean Ubuntu Server 24.04 EC2 checkout. Its relocation guards now reference the categorized tests. It installs test prerequisites, starts pinned Neo4j on localhost, builds and inspects the package, tests the installed wheel, runs the real five-format Chroma/Neo4j procedure, and exercises the CLI lifecycle with isolated generated data. It is a manually invoked development harness, not the P7 installer; static path/syntax checks do not requalify its remote procedure. Use an explicitly approved disposable host and private test configuration before invoking it or the P4 model-evaluation wrappers.

Run it as the normal SSH user from a clean checkout of the exact candidate commit:

```bash
bash deploy/legacy/test-deploy.sh
```

It does not deploy the legacy web server, expose a public port, change AWS security groups, or validate Google/OIDC, MCP authorization, local inference, load, or backup/restore. Test state, generated credentials, logs, and artifacts stay under `~/.local/state/graphmind-test-deploy`, outside the repository. Add a hosted-model key to the private environment file reported by the first run, then use `bash deploy/legacy/test-deploy.sh --with-hosted-model` to include the optional hosted-provider contract.

## Product and compatibility contracts

The supported public entry point is `graphmind serve`: authenticated browser
routes and stateless Streamable HTTP MCP tools share the packaged reader service.
It reads the active SQLite manifest, retrieves eligible Chroma evidence and
Neo4j document-reference expansion, and checks authorization/source eligibility
again before delivery. Readers cannot run host administration or access files
by path. An empty installation is valid and answers with insufficient evidence.

The development-only legacy compatibility adapter `GoTReasoningEngine.reason(query)`
retains these keys; it is not the public product boundary:

```python
{"answer": str, "path": list[str], "knowledge": str, "verification": dict}
```

The engine first retrieves three Chroma matches, then explores only their
Neo4j neighbours for at most seven reasoning decisions. It does not crawl,
index, or mutate either datastore while serving a query. Normal operation
therefore requires a populated Chroma collection and a Neo4j graph built from
the same cleaned corpus.

## What is included

The package contains five-format text ingestion, durable fenced jobs, active-version
Chroma/Neo4j retrieval, Ollama BGE-M3 embedding identity checks, local/hosted
answer providers, a verified browser reader and official-SDK remote MCP candidate.
Operator templates, auth/bootstrap/acceptance guides and network-check tools ship
under the installed environment's `share/graphmind` directory.

The checkout separately retains the legacy MetaKGP crawler/cleaner, BGE-large
indexer, page/fuzzy/semantic graph, LangGraph/MoE experiment and synthetic-QA
runner. These development assets and generated stores are not the generic
product pipeline or bundled user data.

## Project layout

```text
.
├── README.md / pyproject.toml / MANIFEST.in
├── .github/                     # CI; root dotfiles retain standard tool discovery
├── src/graphmind/               # Active installable product, one auth/MCP/reader boundary
│   ├── rag/
│   │   ├── graphmind/           # Current vector + document-reference engine
│   │   ├── tree_navigation/     # Bounded PageIndex/PDF navigation
│   │   ├── multimodal/          # Reserved; not implemented/selectable
│   │   └── components/          # Shared contracts/pipeline/citations/embeddings/vector store
│   └── web/                    # Resource loader; generated static files only in builds
├── assets/web/                  # Single editable CSS/JS source outside src
├── config/                      # Secret-free templates and dependency constraints
├── documentation/               # Public architecture, auth/MCP and dependency guides
├── deploy/                      # Identity assets, acceptance procedures, retired harnesses
├── tools/                       # Build/acceptance commands; evaluation/ helpers
├── tests/                       # contracts/, engines/, unit/, support/, fixtures/, legacy/
└── legacy/                      # Archived prototype code, entrypoints and retained data
```

`legacy/Crawler/cleaned_wiki.jsonl` and `legacy/data/VectorStore/` may already exist in this checkout. They are data artifacts, not source code. Only the legacy prototype requires its matching corpus/stores. The generic package uses separate private runtime roots and an active manifest; do not point it at legacy stores.

See [ARCHITECTURE.md](documentation/ARCHITECTURE.md) for ownership/import boundaries and
[tests/README.md](tests/README.md) for test categories. Old `graphmind.domain`,
`embeddings`, `retrieval` and `storage` imports remain compatibility aliases;
their implementations are not duplicated. Canonical root assets are copied
into the built wheel, so installed browser routes do not require this checkout.
The folder refactor does not implement the two planned engines or improve the
recorded 1/5 semantic baseline. Legacy code/data is retained under `legacy/`;
the original corpus and store bytes were preserved during relocation.

## Legacy development architecture (not the browser/MCP pipeline)

```text
MetaKGP wiki
    │
    ├── legacy/Crawler/crawler.py ──> scraped_wiki.jsonl
    ├── legacy/Crawler/cleaner.py ──> cleaned_wiki.jsonl
    │                              │
    │                ┌─────────────┴──────────────┐
    │                ▼                            ▼
    │      legacy/RAG/indexing.py                legacy/neo4j/neo4j_setup.py
    │      Chroma collection              Page and link graph
    │                │                            │
    └────────────────┴────> legacy/RAG/got_engine.py <──┘
                                  │
              retrieve → explore → synthesize → verify
                                  │
                    Legacy reason() compatibility adapter
```

The legacy reasoning engine begins with three Chroma matches, explores only graph neighbours, and limits traversal to seven reasoning decisions. It sends the collected context to three independent checks—source support, hallucination detection, and logical consistency—before a judge accepts an answer.

## Legacy MetaKGP prototype prerequisites

- Python 3.10 or later.
- A running Neo4j instance (Neo4j 5 recommended).
- An Ollama Cloud API key with access to `gemma4:31b` on the native
  `https://ollama.com` API. Check `/api/tags` for current names; app/CLI cloud
  aliases are not necessarily names advertised by that API.
- Optional CUDA-capable PyTorch installation for faster embedding/indexing work.

On Windows, the evaluation runner imports `pyarrow.dataset` before embedding libraries to avoid a DLL load-order issue. Keep that import first in `tools/evaluation/run_legacy_eval.py`.

## Legacy MetaKGP prototype setup

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r legacy/requirements.txt
Copy-Item config/.env.example .env
```

Edit `.env` with real values:

```dotenv
OLLAMA_API_KEY="..."
NEO4J_URI="bolt://localhost:7687"
NEO4J_USERNAME="neo4j"
NEO4J_PASSWORD="..."

# The legacy prototype is local test code. It is not the authenticated product server.
```

Do not commit `.env`, API keys, OAuth tokens, Neo4j passwords, or a personalized `.mcp.json`.

## Build the legacy MetaKGP test knowledge base

Run these stages in order when creating a fresh corpus. Each command is launched from the repository root.

```powershell
# 1. Discover and scrape MetaKGP pages. Optional integer limits a test crawl.
python -m legacy.Crawler.crawler
python -m legacy.Crawler.crawler 20

# 2. Clean the scraped Markdown and table content.
python -m legacy.Crawler.cleaner

# 3. Create or extend the Chroma collection.
python -m legacy.RAG.indexing

# 4. Import Page nodes and LINKS_TO edges into Neo4j.
python legacy/neo4j/neo4j_setup.py

# 5. Optional: create fuzzy and semantic cross-page relationships.
python legacy/RAG/sparse_graph_mitigation.py
```

The indexer skips a page URL that already exists in Chroma. If you substantially change cleaning or chunking rules, rebuild the vector store deliberately rather than expecting an incremental command to replace existing page chunks.

Treat a corpus refresh as a coordinated operation: rebuild or restore Chroma
and Neo4j from the same `cleaned_wiki.jsonl` input before serving requests.

## How to run the P5/P6 candidate

The supported public process is the installed `graphmind` package. The old
Streamlit, static bearer key, direct Google-token, SSE, and query-token paths
are retired because they bypass current account and revocation policy.

Install the package in a virtual environment, copy `config/.env.example` to a private
`.env`, and configure the GraphMind, Neo4j, Chroma, model, and Keycloak values.
The public base URL must use HTTPS unless it is a loopback development URL.
Secure cookies cannot be disabled for public HTTPS. Keep the account-admin secret
out of the reader environment and use a separate host-account CLI environment.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --constraint config/constraints/core-py313.txt ".[test]"
Copy-Item config/.env.example .env
# Load the private environment by your preferred secret mechanism.
graphmind --dotenv .env serve --host 127.0.0.1 --port 8000
```

The browser reader is at `/`; the remote MCP endpoint is `/mcp`. Browser users
need only a browser. Remote MCP users need a compatible client, with Claude
Code CLI as the first required acceptance client. Both paths use accounts and
tokens issued by the same Keycloak realm. Google is an upstream identity
provider; GraphMind does not accept Google ID tokens as resource credentials.

The MCP tools are `search_documents`, `fetch_document`, and `ask`.
`search_documents` and `fetch_document` let the client use its own model;
`ask` uses the server's configured answer provider. All content routes require
a verified, active account with `graphmind:read` and the exact `/mcp` audience.
See [AUTH_AND_MCP.md](documentation/AUTH_AND_MCP.md) for the route, token, account-state, and
Claude Code candidate contracts.

Host operators manage account state locally:

```powershell
graphmind accounts list
graphmind accounts disable reader@example.test
graphmind accounts enable reader@example.test
graphmind accounts delete reader@example.test
graphmind accounts restore reader@example.test
```

`delete` is a retained soft deletion: it disables the account, records deleted
status, and revokes sessions without erasing its email or credential record.
Readers cannot upload documents or change configuration. Host-only document
administration continues through the existing `graphmind import`, worker,
status, reindex, and delete commands.

`deploy/legacy/deploy.sh` is deliberately retired because it installed the superseded auth
path. Internet-facing Ubuntu service installation, reverse proxy, Keycloak,
PostgreSQL, SMTP, backup, and reboot automation belong to the P7 operator
release. Do not use the current candidate as a production deployment. The recorded
P5/P6 functional checkpoint passed; new deployments and affected changes still
need their relevant real identity/browser/client checks. Follow the tracked
[same-candidate acceptance matrix](deploy/acceptance/README.md); unavailable
inputs or already-issued-token network calls cannot substitute for those gates.

## Evaluate the legacy prototype

```powershell
python tools/evaluation/run_legacy_eval.py
```

The runner creates synthetic question/answer pairs from cleaned pages when no dataset is present, evaluates the configured number of cases, checkpoints progress after each successful evaluation, and writes JSON and Markdown reports under `legacy/reports/evaluation/`. It is intentionally sequential by default to respect Ollama Cloud rate limits.

Evaluation uses an LLM-generated dataset and LLM-as-a-judge scoring. Treat the results as regression indicators, not an independent factual benchmark. Add human-reviewed and out-of-domain cases before making production accuracy claims.

`tools/evaluation/run_legacy_eval.py` and its artifact filenames belong to the legacy prototype. The current
number of evaluated cases is controlled by `NUM_QUESTIONS` in that script;
check its value rather than inferring the count from the filename.

## Test and validate

```powershell
python -B -m unittest discover -s tests -t . -v
```

Use CPython 3.13 (`>=3.13,<3.14`) and install with `config/constraints/core-py313.txt`.
The offline suite uses synthetic identities/adapters and temporary stores; it also
runs actual local Chroma subprocess and host OS-lock regressions. It does not
contact live Neo4j, Keycloak, SMTP, Google or Ollama. Node is test-only, required
for the executable browser-script network/JSON-failure regression.

```powershell
python tools/verify_package.py
```

This builds an allowlisted snapshot, inspects wheel/sdist, creates a clean
temporary venv, installs constrained dependencies, checks imports outside the
checkout and runs the installed-wheel suite. CI does this on hosted Ubuntu and
Windows. The separate manual `real-stores` workflow actually tests disposable
Chroma/Neo4j; it is not identity/client/model or native-service acceptance. A
skipped/unavailable integration is not a pass. No current CI run is claimed by
changing the workflow. See [the acceptance guide](deploy/acceptance/README.md)
for opt-in real tests and the remaining manual matrix.

## Operational notes (legacy development assets)

- The crawler waits between requests and restricts itself to MetaKGP content pages.
- Course URLs containing colons are retained; only non-content MediaWiki namespaces are excluded.
- Vector and graph storage are independent. Rebuild or restore both together when changing the source corpus.
- Query latency is dominated by remote LLM calls and the four-stage verification flow.
- Neo4j lookup values are parameterized, and the reasoning prompts explicitly treat user input as untrusted data.
- The retired Streamlit sidebar was not a dependency-health check. Product
  `/health/ready` uses fresh bounded dependency checks, not the legacy indicators;
  full manifest audits remain host-only `graphmind doctor` work.
- The optional sparse-graph command computes pairwise title similarity and is
  an offline operation, not part of normal serving.

## Troubleshooting

| Symptom | Likely cause | Action |
| --- | --- | --- |
| `Input file ... not found` | A previous ingestion stage has not run. | Run the crawler, then the cleaner. |
| No relevant answers | Chroma or Neo4j is empty, unavailable, or out of sync. | Verify both stores were built from the same cleaned corpus. |
| `OLLAMA_API_KEY is not configured` | `.env` is missing or not loaded. | Create `.env` from `config/.env.example` and set the key. |
| Windows access violation during evaluation | Embedding-library DLL ordering issue. | Keep the top-level `pyarrow.dataset` import in `tools/evaluation/run_legacy_eval.py`. |
| `401 Unauthorized` from MCP | Missing, expired, unverified or revoked resource grant. | Use the configured client's Keycloak OAuth login; static keys and direct Google tokens are rejected. |
| `503 identity_unavailable` | Current identity authority cannot be reached/validated. | Restore the private identity dependency and retry; do not replace OAuth with a shared token. |

## Development principles

Keep source modules small and side-effect free where possible, keep generated stores out of code changes, remove only verified dead or duplicated code, and preserve the legacy compatibility keys returned by `GoTReasoningEngine.reason()` without exposing that prototype as the product boundary:

```python
{"answer": str, "path": list[str], "knowledge": str, "verification": dict}
```
