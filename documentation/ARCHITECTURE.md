# Codebase structure and ownership

The product is `src/graphmind`. One installation serves one organization through
one browser/MCP boundary. Legacy `legacy/Crawler/`, `legacy/RAG/`, `legacy/neo4j/` and their datasets
remain separate development assets; they are not imported into the product or
included in its distributions by this refactor.

## Repository groups

Keep product code in `src/`, editable public assets in `assets/`, tests in
`tests/`, public guides in `documentation/`, secret-free configuration and
constraints in `config/`, and operator/build commands in `deploy/` and `tools/`.
Legacy experiments, retired entrypoints and retained original stores live under
`legacy/`, not beside or inside the product. Root `.env`, owner settings and
ignored private plans are not relocated or published. Root `.mcp.json` and
scanner/build metadata remain where their tools discover them.

## Canonical modules

| Responsibility | Owner |
| --- | --- |
| Configuration and application composition | `config.py`, `service.py`, `cli.py` |
| Identity/account policy, browser and MCP | `identity.py`, `product_server.py`, `reader.py` |
| Source staging, parsing, durable jobs and publication | `ingestion.py`, `parsing.py`, `parser_worker.py`, `files.py`, `metadata.py`, `jobs.py` |
| Answer-provider transport | `providers.py`, `http_client.py` |
| Shared records and retrieval protocol | `rag/components/contracts.py` |
| Derived-index recipes, inventories and publication heads | `rag/components/index_contracts.py`, `indexes.py` |
| Shared admission, answer orchestration and citation membership | `rag/components/pipeline.py`, `citations.py` |
| Shared embedding and vector-store implementations | `rag/components/embeddings.py`, `vector_store.py` |
| Current vector/reference retrieval and reference-graph adapters | `rag/graphmind/engine.py`, `reference_graph.py` |
| Engine catalogue facts, not readiness or dispatch | `rag/registry.py` |
| Fixed browser-resource loading | `web/assets.py` |

`rag/tree_navigation/` implements bounded text-bearing PDF navigation through
the pinned local PageIndex adapter. `rag/multimodal/` contains the local PDF-page
adapter; its reader catalogue remains disabled pending qualification. Engines
must be explicitly enabled; catalogue membership does not
download models or enable a runtime adapter.

## Dependency direction and compatibility

The composition root connects the implemented engine to the shared answer
pipeline. The pipeline accepts the `EvidenceRetriever` protocol; it does not
import a concrete engine. Components do not import browser/MCP routes or their
authentication code. All transports continue through the same reader and
identity authority, with the existing least-privilege credential separation.

Root `domain.py`, `embeddings.py`, `retrieval.py` and `storage.py` are thin
compatibility exports for existing consumers, not second implementations.
Internal product imports use canonical modules. The compatibility exports retain
the same type and enum identities. No corpus migration or reindex is needed
merely because Python modules moved. Versioned engine recipes and the additive
metadata schema changes below have their own explicit lifecycle boundaries.

Reuse the existing provider, source authority, jobs and citation policy rather
than copying them into each engine. Algorithm-specific parsers and index shapes
need not be forced into the current vector representation. Future upstream code
normally stays in pinned packages, accessed through small adapters; a vendored
component/fork needs explicit provenance, notices and maintenance ownership.

Immediate host import/reindex/delete and evaluation imports select their submitted
job through the existing durable executor. Targeted execution shares the kernel
lock, lease/attempt fencing, heartbeat and publication handler with FIFO workers;
it does not drain unrelated jobs or add a second ingestion authority. Completed
jobs are idempotent, interrupted attempts reconcile under the lock, and pending
or failed submissions cannot be labelled successful completion.
Targeted claims also preserve ordering within a document: an earlier pending
version blocks the later submission with `job_not_ready`, rather than allowing
a subsequent worker to roll the document back to the older queued version.

## Original-file identity

New imports hash the exact original bytes before combining the content hash
with extraction/index fingerprints to identify a version. Replacing a PDF chart
while retaining the same extracted text therefore creates a new version. The
previous original and active version remain available until the replacement
passes publication checks. Identical source bytes and fingerprints remain
idempotent. Historical versions are retained without automatic migration or
reimport; their older content hashes may describe extracted text.

`sources.py` applies the existing metadata authority's active-version policy for
the answer pipeline and shared reader. Search results and model input must refer
to current versions; after generation, every version supplied as context is
checked again, including uncited passages. Original-byte snapshots use bounded
private-file reads and verify the recorded hash before use. Build snapshots also
require the existing durable job/writer attempt fence. A historical text-derived
hash that cannot authenticate original bytes requires explicit host reimport for
byte-sensitive consumers; the service never rewrites that history automatically.

## Derived-index lifecycle

SQLite schema 3 adds per-engine indexes and current index heads to the existing
metadata database, plus bounded immutable JSON input on the existing durable jobs.
Recipes identify index-affecting settings; changing a recipe creates another
derived index attached to the same source version. Indexes move through staged,
building, ready, failed and invalidated states under the existing writer/attempt
fence. Source publication and selected ready heads commit in one transaction;
a failed replacement preserves the previous source and usable head. Tombstones
immediately deny eligibility, before the deletion job cleans stores and invalidates
the index records. Multi-engine imports stage one original and publish all
selected ready heads with the source in that transaction. Adding a derived
recipe can reuse an unchanged active source version and its original.

The schema 3 upgrade registers metadata-verified legacy GraphMind chunk inventories
without opening originals, vector stores or graph stores. It preserves their
existing storage layout. GraphMind import/retry/delete jobs now use this lifecycle;
the tree adapter shares the same lifecycle. A derived artifact reference records a
private manifest identity; its presence alone does not qualify a backend or prove
that its artifact bytes have been verified.

Schema 4 adds a derived FTS5 lexical index in the same database. The opt-in
`retrieval_profile="hybrid"` builds `hybrid-context-v1` indexes with deterministic
source title/locator context in a separate Chroma path/collection. BM25 and dense
ranks are fused before a local cross-encoder ranks at most 50 passages. Explicit
references are collected across selected source versions; another filtered search
selects relevant passages inside at most the configured number of target documents.
Evidence retains its original passage and locator instead of an 800-character
prefix. Exact repeated passages are collapsed and evidence is bounded per source.

Hybrid mode requires an explicitly retained, verified
`BAAI/bge-reranker-v2-m3` directory in `reranker_dir`. It performs no automatic model
download or hosted fallback. The worker loads safetensors locally, verifies the
retained inventory, uses CPU inference and exits after each call to release weights.
Calls share a host-local inference lock. Set `retrieval_seed_limit=8` for the initial
hybrid recipe; existing evidence and context limits still apply. Hybrid references
use document scope; the legacy chunk-scope setting does not change this algorithm.

Import/reindex under the selected profile builds its recipe through the existing
executor. A recipe can attach to the same unchanged source version without
overwriting legacy vectors or originals. Switching profiles requires explicitly
indexing the selected profile's sources; missing indexes fail clearly. The default
remains `legacy` pending measured quality and release acceptance. These mechanisms
do not establish an accuracy gain or native service/release qualification.

## Vectorless PDF navigation

Enable `tree_navigation` alone or alongside GraphMind with `enabled_engines`.
Its local subprocess calls PageIndex Flash with both summaries and optimization
disabled, so indexing makes no model call. The adapter preserves its hierarchy
and original extracted page text, adds bounded page navigation, and stores the
verified representation in the shared private artifact store. Tree-only import,
query and deletion do not resolve an embedding model or access vector/graph stores.

Queries select one ready PDF through the same reader context. The configured
answer provider selects only supplied node IDs for at most six navigation steps,
then the common answer pipeline receives at most three original pages with
page-specific citations. Unknown nodes, excessive context, missing indexes and
unsupported formats fail clearly. The adapter never falls back to vector search.
Local Ollama calls and model workers share an installation lock; local requests
unload Ollama weights after use. Workers cannot open outbound network connections.

## Original PDF-page retrieval

The optional `multimodal` adapter uses a retained, pinned ColQwen2 model through
Sentence Transformers' multi-vector encoder. Indexing rasterizes original PDF
pages in a bounded offline PDFium worker and stores page vectors as safetensors
alongside verified PNGs in the shared private artifact store. It accepts
text-bearing PDFs, up to 50 pages, including visual-only pages within them.
There is no OCR or fallback to text/vector retrieval.

A query selects one ready PDF and sends its highest-ranked original page to the
existing local vision-capable Ollama provider. Image input is restricted to local
loopback endpoints; hosted providers and the diagnostic extractive provider are
rejected. Page numbers and source identities appear in citations; image bytes
remain private and are omitted from JSON results. This bounded adapter does not
establish factual quality or native release acceptance by itself.

The authenticated reader catalogue reports readiness per document and engine.
Browser and MCP readers share this catalogue and source checks. Original-file
downloads and PNG page previews recheck both authorization and the active source
after reading, before returning bytes. Raw originals are attachments; previews
render bounded PNGs rather than executing document content.

## Assets and builds

`frontend/` owns the React/TypeScript reader components and common API client.
Its pinned Vite build generates static CSS/JavaScript in `assets/web/`, alongside
third-party notices and a source/output hash manifest. See `frontend/README.md`
for build commands. Node is a build/test prerequisite, not a production server.
The setuptools build hook verifies that manifest and copies an explicit allowlist
into `graphmind/web/static/`; it never installs npm packages or runs a frontend
build. The source archive retains the canonical frontend inputs and generated
assets so it can build a wheel without npm or package-index access.

Source runs resolve only the explicit checkout asset directory. Installed wheels
use package resources; they must not fall back to arbitrary working-directory
files or remote URLs. Asset URLs include a content-derived version to avoid a
stale reader after an update. The UI shares one API client, engine/document
selection, answer panel and protected source preview. Cancelling or changing
selection discards late responses; account loss clears the current answer and
preview. Only an authentication failure before execution may renew and replay
a request; provider errors and revocation never trigger a hidden model retry.

`tools/verify_package.py` builds an allowlisted snapshot, checks wheel/sdist
contents, installs outside the checkout, verifies CLI/resource loading and runs
the offline tests against the installed package. Tests, fixtures, private plans,
credentials, original corpora and runtime databases are not shipped.

## Tests and scope

All test code and fixtures live in root-level `tests/`, categorized as documented
in `tests/README.md`. Shared fixtures live in `tests/support/`; test modules do
not import other test modules for setup. Integration and native service checks
are explicit, not counted as offline passes or represented by placeholder tests.

All engines reuse the existing identity, source, job, provider and reader
authorities. Ingestion remains host-only. GraphMind factual quality, complete
native services and final security/licensing/release qualification are separate
from offline contract tests and individual model probes.
