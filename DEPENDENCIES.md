# Dependency Inventory

Updated: 2026-09-14

This inventory separates the installable GraphMind package from development-only
legacy code and external model or service components. It records dependency facts;
it is not a licensing approval. Final license compatibility and notices remain a
release decision.

## Installable package

The package metadata in `pyproject.toml` currently declares:

| Purpose | Direct dependency |
| --- | --- |
| Vector store | `chromadb==1.5.9` |
| Graph client | `neo4j==6.3.0` |
| PDF text extraction | `pypdf==6.18.0` |
| Test/build command | `build==1.6.0` |
| Build backend | `setuptools==80.9.0` |
| Wheel construction | `wheel==0.45.1` |

[`constraints/core-py313.txt`](constraints/core-py313.txt) captures all 84 direct,
build, test, and transitive distributions resolved in a clean CPython 3.13 Windows
x86-64 environment with pip 26.2.1. Installation and `pip check` passed on
2026-09-14. This file is an auditable resolver snapshot, not yet the release lock:
P4 must install the same snapshot on Ubuntu and Windows or record the smallest
platform-specific split required by wheel availability.

Most transitive packages enter through Chroma, including its HTTP, ONNX Runtime,
OpenTelemetry, Kubernetes-client, tokenization, validation, and CLI stacks. Neo4j
adds `pytz`; pypdf has no active runtime dependency in this resolution; `build`
adds packaging and build-hook support. `oauthlib` and `requests-oauthlib` appear
transitively through Chroma's Kubernetes client. Their presence does not implement
or select GraphMind user authentication.

## P4 model and embedding baseline

| Component | Accepted candidate | Dependency boundary |
| --- | --- | --- |
| Local answer runtime | Ollama `0.34.0` | External same-host service; install and artifact hashes must be recorded per platform. |
| Local answer model | `gemma4:e2b`, Ollama digest prefix `7fbdbf8f5e45`, Q4_K_M | Pulled model artifact, approximately 7.2 GB; not embedded in the Python wheel. |
| Hosted answer model | Deployment-configured Ollama-compatible hosted model; Gemma 4 and Nemotron are candidates | External service and operator-selected model ID; P4 must record the exact provider/model used for each result. |
| Production embedding family | `BAAI/bge-m3`, dense output dimension 1024 | External model artifact; model revision and embedding fingerprint must be immutable within an index. |
| Requested embedding optimization | FP8 | Target for P4 measurement, not a verified upstream artifact or runtime choice yet. |

The official BAAI examples document BGE-M3 FP16 inference. The current official
Ollama BGE-M3 artifact is also FP16. No official BAAI FP8 artifact was identified
during this inventory, so the repository does not invent an FP8 package or model
identifier. P4 must compare a reproducible baseline with an identified FP8 build,
or record that the selected hardware/runtime requires FP16 or INT8 instead. The
base model choice remains `BAAI/bge-m3`.

Model runtimes and weights are distribution inputs, not ordinary Python
transitives. They need independent version, checksum, source, terms, platform,
memory, latency, and egress records. Hosted model names alone do not establish
reproducibility because providers can update an endpoint behind a name.

## P5/P6 browser, OAuth, and MCP group

The user moved the MCP/OAuth design spike and implementation to P5/P6. These
components are therefore inventoried as pending and are not package dependencies
yet:

| Area | Current evidence | Pinning point |
| --- | --- | --- |
| Upstream identity | Google OpenID Connect | P5 selects the maintained authorization component and pins its dependencies. |
| Browser/API service | Legacy prototype uses FastAPI/Uvicorn | P5 decides whether those components remain and pins the chosen versions. |
| Password hashing/email verification | Contract accepted; implementation absent | P5 selects memory-hard hashing and mail/token dependencies. |
| MCP SDK | Legacy requirements contain unpinned `mcp`; current stable Python SDK observed as `2.2.0` | P6 pins the SDK and protocol revision after the authenticated-client design is implemented. |
| Token validation | Legacy prototype uses unpinned `python-jose[cryptography]` | P5/P6 choose the maintained GraphMind-token validation path; legacy presence is not acceptance. |

The P5/P6 resolver snapshot must be generated from a clean environment after these
choices are implemented. It must not reuse the unrelated OAuth libraries already
pulled by Chroma.

## Legacy and development-only requirements

The root `requirements.txt` remains an unpinned legacy prototype list. It includes
the crawler (`httpx`, Beautiful Soup, markdownify), data preparation (pandas,
lxml, pyarrow), model/embedding clients (`ollama`, sentence-transformers), legacy
RAG orchestration (LangChain, LangGraph, NetworkX), Streamlit UI, FastAPI/Uvicorn,
MCP, JOSE, and supporting packages.

Those entries are not dependencies of the `graphmind-rag-mcp` wheel and are not a
release lock. P4 may promote only the model and embedding dependencies actually
used by the accepted implementation. P5/P6 do the same for browser, account,
OAuth, and MCP dependencies. The crawler and MetaKGP corpus remain test and
development assets outside the final user package.

## Maintenance rule

For every dependency change:

1. Update the direct pin and regenerate the clean resolver snapshot.
2. Run `pip check`, offline tests, and the relevant real integration on Ubuntu and
   Windows.
3. Record model/service versions and hashes separately from Python packages.
4. Review licenses and distribution terms before release packaging.
5. Inspect the final wheel and source archive so development assets, model weights,
   credentials, and private planning files are absent.
