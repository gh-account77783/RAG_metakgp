# Archived prototype and retained data

This directory isolates the original crawler, Graph-of-Thoughts experiments,
manual evaluation code, retired entrypoints and retained local data from the
active `src/graphmind/` product. It is excluded from product distributions.

- `Crawler/`, `RAG/`, `neo4j/`: historical Python code; module imports use
  `legacy.Crawler` / `legacy.RAG`. Do not add a neo4j/__init__.py that shadows the driver.
- `data/`: original VectorStore and VectorStore_test files, relocated without
  reindexing, database writes or content changes.
- `entrypoints/`: fail-closed retired UI/MCP guards, not alternate servers.
- `test_RAG/`, `reports/`, `cache/`: preserved historical material.
- `requirements.txt`: unpinned legacy-only dependencies, not product requirements.

Run any archived network/model/indexing command explicitly from the checkout
with the intended private configuration. Do not install its broad dependencies
into the product environment or point it at active GraphMind data. Default
product tests import only the inert text helpers and frozen corpus fixture;
they do not launch the crawler, call hosted models or open the retained stores.
The source corpus and existing binary stores were checksum-verified during
relocation. Original frozen evaluation bytes/thresholds remain unchanged.

The standalone legacy evaluator is now `tools/evaluation/run_legacy_eval.py`;
P4 wrappers are alongside it. Historical isolated deployment scripts live in
`deploy/legacy/` and are not production installers. Credentials remain private
and are not stored in this archive.
