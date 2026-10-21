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

`rag/tree_navigation/` and `rag/multimodal/` reserve the agreed integration
namespaces. They contain no fake engine, hidden fallback or skipped acceptance
test: both remain unimplemented and unavailable. Only GraphMind runs today.

## Dependency direction and compatibility

The composition root connects the implemented engine to the shared answer
pipeline. The pipeline accepts the `EvidenceRetriever` protocol; it does not
import a concrete engine. Components do not import browser/MCP routes or their
authentication code. All transports continue through the same reader and
identity authority, with the existing least-privilege credential separation.

Root `domain.py`, `embeddings.py`, `retrieval.py` and `storage.py` are thin
compatibility exports for existing consumers, not second implementations.
Internal product imports use canonical modules. Types and enum identities,
serialized results, configuration, index fingerprints, storage schemas and
existing ranking/budgets remain unchanged. No corpus migration or reindex is
needed merely because Python modules moved. Accuracy improvements and new
engine indexing/lifecycle contracts remain separate implementation work.

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
the index records.

The schema 3 upgrade registers metadata-verified legacy GraphMind chunk inventories
without opening originals, vector stores or graph stores. It preserves their
existing storage layout. GraphMind import/retry/delete jobs now use this lifecycle;
other engine adapters remain unavailable. A derived artifact reference records a
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
do not establish an accuracy gain, native service qualification or working tree/
visual engines.

## Assets and builds

`assets/web/` is the single editable CSS/JS source, outside `src/`. The pinned
setuptools build hook copies an explicit allowlist into the built package's
`graphmind/web/static/`. Those are generated distribution resources, not a
second checked-in source copy. The source archive includes the hook and original
assets, so building a wheel from it works too.

Source runs resolve only the explicit checkout asset directory. Installed wheels
use package resources; they must not fall back to arbitrary working-directory
files or remote URLs. The current reader has not been migrated to React. Its
future build can use the same deliberate static-resource boundary.

`tools/verify_package.py` builds an allowlisted snapshot, checks wheel/sdist
contents, installs outside the checkout, verifies CLI/resource loading and runs
the offline tests against the installed package. Tests, fixtures, private plans,
credentials, original corpora and runtime databases are not shipped.

## Tests and scope

All test code and fixtures live in root-level `tests/`, categorized as documented
in `tests/README.md`. Shared fixtures live in `tests/support/`; test modules do
not import other test modules for setup. Integration and native service checks
are explicit, not counted as offline passes or represented by placeholder tests.

This refactor adds no new third-party dependency, model call, architecture
selection API, authentication server, public ingestion route or data migration.
Tree/visual implementation, GraphMind factual quality, complete native services
and final security/licensing/release qualification remain separate gates.
