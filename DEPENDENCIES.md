# Dependency Inventory

Updated: 2026-10-03

This inventory separates the installable GraphMind package from development-only
legacy code and external model or service components. It records dependency facts;
it is not a licensing approval. Final license compatibility and notices remain a
release decision.

## Installable package

The package metadata in `pyproject.toml` currently declares:

Supported interpreter baseline is CPython 3.13 (`>=3.13,<3.14`), deliberately
narrowed from the unqualified >=3.11 claim. This does not claim native OS service
support. Offline CI now installs under the snapshot on Ubuntu/Windows and runs
clean built-wheel verification; actual runs on the changed candidate still need
CI/EC2 evidence. Node is a test-only prerequisite for the browser-script fault
regression, not an application runtime dependency.

| Purpose | Direct dependency |
| --- | --- |
| Vector store | `chromadb==1.5.9` |
| Graph client | `neo4j==6.3.0` |
| PDF text extraction | `pypdf==6.18.0` |
| MCP protocol/resource server | `mcp==2.0.1` |
| OIDC ID-token validation | `PyJWT==2.14.0` |
| Browser/ASGI routes | `starlette==1.6.0` |
| ASGI process | `uvicorn==0.53.0` |
| Test/build command | `build==1.6.0` |
| Build backend | `setuptools==80.9.0` |
| Wheel construction | `wheel==0.45.1` |

[`constraints/core-py313.txt`](constraints/core-py313.txt) captures all 97 direct,
build, test, and transitive distributions resolved for CPython 3.13 Windows
x86-64 with pip 26.2.1. A clean constrained Windows virtual-environment install
and `pip check` passed on 2026-09-16. This file is an auditable resolver snapshot,
not yet the release lock: Ubuntu, Windows 11, and Windows Server 2025 must each
clean-install it or record the smallest platform-specific split required by wheel
availability.

`constraints/linux-py313.txt` adds `uvloop==0.22.1`, the Linux-only dependency
of Chroma's `uvicorn[standard]` extra. Its [published CPython 3.13 Linux wheels](https://pypi.org/project/uvloop/0.22.1/)
and installed Uvicorn metadata support this overlay; actual Ubuntu resolution,
installation and runtime remain acceptance checks, not inferred passes. Apply
both constraints files. Inventory tests follow extras/markers to require exact
pins across the Windows and Linux dependency closures.

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

P4 uses Python's standard-library HTTP client for the Ollama-compatible embedding
and answer adapters. It therefore adds no direct Python distribution to the
CPython 3.13 resolver snapshot. The adapter records the full digest, reported
precision, dimension, endpoint mode, model tag, and request budgets in evaluation
reports. An embedding identity mismatch fails closed and requires an explicit
reindex; an answer-model change does not change the vector index.

The official Ollama `bge-m3` tag currently reports FP16. The implementation
accepts `auto` and records that observed precision. If an operator requests FP8
while the runtime reports another precision, startup/readiness fails rather than
recording a false FP8 claim. An exact FP8 artifact remains an open measured
alternative, not an implemented dependency.

## P5/P6 browser, OAuth, and MCP group

| Area | Selected candidate | Boundary |
| --- | --- | --- |
| Credential/account authority | Keycloak `26.7.3` | External same-host native Java service; owns passwords, verification, Google links, status, grants, signing keys and migrations. |
| Identity database | PostgreSQL `17` | External same-host service; Keycloak's development-file database is excluded from production. |
| Upstream identity | Google OpenID Connect through Keycloak | Google tokens are never accepted directly by GraphMind. Exact test client and callback remain deployment secrets. |
| Browser/API service | Starlette `1.6.0` through MCP SDK ASGI app | Stateless GraphMind process; Keycloak introspection checks every request. |
| MCP SDK | `mcp==2.0.1` | Streamable HTTP and RFC 9728 protected-resource discovery; 2026-07-28 plus 2025-11-25 compatibility. |
| Token validation | Keycloak RFC 7662 introspection plus `PyJWT==2.14.0` ID-token validation | Exact issuer, resource audience, scope, expiry, verified email, nonce and active account are enforced. |
| Initial MCP client | Claude Code public PKCE client on callback port 8765 | Anonymous dynamic registration is disabled; independent SDK/protocol acceptance remains required. |

The Python group adds 13 resolver entries over the earlier 84-package snapshot:
`mcp`, `mcp-types`, `httpx2`, `httpcore2`, `truststore`, `PyJWT`, `cryptography`,
`cffi`, `pycparser`, `python-multipart`, `sse-starlette`, `starlette`, and Windows'
conditional `pywin32`; normal resolver movement also refreshed a few existing
pins. Starlette and Uvicorn are direct pins because GraphMind imports them. PyJWT
is a direct pin; MCP's crypto extra supplies the signature backend.

Keycloak, PostgreSQL, Java, Google, SMTP, and Claude Code are service/runtime
dependencies rather than Python wheel dependencies. Their exact artifacts,
hashes, licenses, memory and native service behavior remain real-environment
acceptance evidence. The realm template and route/client contract live in
`deploy/keycloak/` and `AUTH_AND_MCP.md`.

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
