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
