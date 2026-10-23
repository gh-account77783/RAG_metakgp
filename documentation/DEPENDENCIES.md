# Dependency Inventory

Status reconciled: 2026-10-23 (core and optional profiles)

This inventory separates the installable GraphMind package from development-only
legacy code and external model or service components. It records dependency facts;
it is not a licensing approval. Final license compatibility and notices remain a
release decision.

## Installable package

The package metadata in `pyproject.toml` currently declares:

Supported interpreter baseline is CPython 3.13 (`>=3.13,<3.14`), deliberately
narrowed from the unqualified >=3.11 claim. This does not claim native OS service
support. Offline CI now installs under the snapshot on Ubuntu/Windows and runs
clean built-wheel verification. Exact commit `587140a` passed both OS jobs and
secret scan in [CI39461334545](https://github.com/gh-account77783/RAG_metakgp/actions/runs/39461334545).
This is not native service/reboot or release qualification. Node is a frontend
build/test prerequisite, not a production application runtime dependency.

| Purpose | Direct dependency |
| --- | --- |
| Application HTTPS trust bundle | `certifi==2026.10.19` |
| Vector store | `chromadb==1.5.9` |
| Graph client | `neo4j==6.3.0` |
| PDF text extraction | `pypdf==6.18.0` |
| MCP protocol/resource server | `mcp==2.0.1` |
| OIDC ID-token validation | `PyJWT==2.15.1` |
| Browser/ASGI routes | `starlette==1.6.0` |
| ASGI process | `uvicorn==0.53.0` |
| Test/build command | `build==1.6.0` |
| Build backend | `setuptools==80.9.0` |
| Wheel construction | `wheel==0.45.1` |

[`config/constraints/core-py313.txt`](../config/constraints/core-py313.txt) captures the 97-distribution core baseline (direct,
build, test and transitive) originally resolved for CPython 3.13 Windows
x86-64 with pip 26.2.1. A clean constrained Windows virtual-environment install
and `pip check` passed on 2026-09-16. This file is an auditable resolver snapshot,
not yet the release lock: Ubuntu, Windows 11, and Windows Server 2025 must each
clean-install it or record the smallest platform-specific split required by wheel
availability.

`config/constraints/linux-py313.txt` adds `uvloop==0.22.1`, the Linux-only dependency
of Chroma's `uvicorn[standard]` extra. Its [published CPython 3.13 Linux wheels](https://pypi.org/project/uvloop/0.22.1/)
and installed Uvicorn metadata support this overlay. Actual Ubuntu constrained
resolution/install tests pass in recorded EC2 and exact-commit CI evidence;
full native services/release qualification remain separate. Apply
both constraints files. Inventory tests follow extras/markers to require exact
pins across the Windows and Linux dependency closures.

The optional engine profile retains `PyJWT==2.15.1` and `websockets==16.0`.
These replaced the earlier 2.14.0/17.1 core pins for the former Agents 0.24
profile. The current Agents 0.20 rollback also requires websockets <17
([published metadata](https://pypi.org/pypi/openai-agents/0.20.0/json)). The original September environment/CI
receipts qualify their original pins; they are not evidence for this adjustment.
Current native/package receipts must identify the exact bundle and interpreter.

The core constraint uses `certifi==2026.10.19` for the published CA trust update.
Python's [3.13.17 security release](https://www.python.org/downloads/release/python-31317/)
also updates its bundled pip/CA certificates for the emSign incident. Retained
older bundles qualify their recorded bytes; fresh candidates must qualify the
updated CA wheel and interpreter separately. Do not modify certificate bundles
manually or disable TLS verification to make an installation succeed.
Model and identity urllib clients share a verified SSL context loaded explicitly
from this unmodified public CA bundle, with hostname checking enabled. Application
HTTPS does not merge per-host trust stores; loopback HTTP remains available for
the private same-host development/service bindings.

## Optional inference profiles

| Package extra | Direct libraries |
| --- | --- |
| `tree` | `pageindex==0.4.4`, `Pillow==13.0.0` |
| `rerank` | `sentence-transformers==6.1.0`, `torch==2.14.1` |
| `visual` | `sentence-transformers==6.1.0`, `torch==2.14.1`, `torchvision==0.29.1`, `transformers[vision]==5.20.2`, `pypdfium2==5.14.0` |

Installing an extra supplies dependencies; it does not enable or implement a
reserved engine. The runtime catalogue records GraphMind and bounded PDF tree
navigation as implemented; visual catalogue qualification remains separate.
The base install does not require these libraries. Each profile shares the same
application identity, reader, MCP, source and job boundaries.

Apply `config/constraints/engines-py313.txt` with the core and Linux constraints
when resolving any inference extra. Its 39 additional exact pins come from the
combined resolver closure, now including `openai-agents==0.20.0`,
`openai==2.54.0` and `litellm==1.106.2`. LiteLLM's published metadata requires
OpenAI >=2.20,<3; Agents 0.20 accepts OpenAI >=2.45,<3 and MCP <3. The former
Agents 0.24/OpenAI 3 pins cannot resolve with that LiteLLM release. This is a
resolver input, not a complete hash lock. Use
[retained native bundles](RELEASE_BUNDLES.md) for repeatable offline installation.

LiteLLM 1.107.0 was [compromised and withdrawn upstream](https://docs.litellm.ai/blog/security-incident-oct-21-2026).
Do not install it from a retained wheelhouse or restore an environment containing
it. Earlier hash-verified bundles containing that version are revoked: matching
hashes and passing tests do not establish software safety. The constraint now
uses the upstream-recommended 1.106.2; this change still requires fresh bundle
qualification. If 1.107.0 was installed, do not start that environment's Python
or pip. Follow the incident advisory for containment, credential response and
rebuilding on a clean host; changing a dependency pin does not clean an exposed
host.

The reviewed x86-64 CPython 3.13 CPU artifacts and publisher hashes are in
`config/artifacts/cpu-torch-cp313.json`. Supply those downloaded local wheels to
the collector with `--artifact`; the constraints require their `+cpu` versions.
Do not let a generic Linux PyPI torch install select CUDA dependencies on a
CPU-only host. Windows 11 and Server 2025 still need separate native acceptance
although they share compatible wheel tags. Neither metadata resolution nor an
upstream import smoke establishes GraphMind engine/service/quality acceptance.

Models remain separate revision-pinned assets with independently verified file
hashes and terms. Model cards, backbone terms and bundled notices require rights
review. Use verified local paths with downloads and remote code disabled in
runtime adapters; no upstream cloud/MCP/server boundary is selected by an extra.

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
| Credential/account authority | Keycloak `26.7.5` | External same-host native Java service; owns passwords, verification, Google links, status, grants, signing keys and migrations. |
| Identity database | PostgreSQL `17` | External same-host service; Keycloak's development-file database is excluded from production. |
| Upstream identity | Google OpenID Connect through Keycloak | Google tokens are never accepted directly by GraphMind. Exact test client and callback remain deployment secrets. |
| Browser/API service | Starlette `1.6.0` through MCP SDK ASGI app | Stateless GraphMind process; Keycloak introspection checks every request. |
| MCP SDK | `mcp==2.0.1` | Streamable HTTP and RFC 9728 protected-resource discovery; 2026-07-28 plus 2025-11-25 compatibility. |
| Token validation | Keycloak RFC 7662 introspection plus `PyJWT==2.15.1` ID-token validation | Exact issuer, resource audience, scope, expiry, verified email, nonce and active account are enforced. |
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
`deploy/keycloak/` and `documentation/AUTH_AND_MCP.md`.

## Legacy and development-only requirements

The root `legacy/requirements.txt` remains an unpinned legacy prototype list. It includes
the crawler (`httpx`, Beautiful Soup, markdownify), data preparation (pandas,
lxml, pyarrow), model/embedding clients (`ollama`, sentence-transformers), legacy
RAG orchestration (LangChain, LangGraph, NetworkX), Streamlit UI, FastAPI/Uvicorn,
MCP, JOSE, and supporting packages.

Those entries are not dependencies of the `graphmind-rag-mcp` wheel and are not a
release lock. P4 may promote only the model and embedding dependencies actually
used by the accepted implementation. P5/P6 do the same for browser, account,
OAuth, and MCP dependencies. The crawler and MetaKGP corpus remain test and
development assets outside the final user package.

## Static reader and local answer context

The reader build uses React/React DOM 19.3.1, TypeScript 7.0.2 and Vite 8.3.5
with Node 24.22.1. `frontend/package-lock.json` retains exact public npm artifact
integrities. `assets/web/THIRD_PARTY_NOTICES.txt` contains the retained software
notices; its presence is not a blanket software/model/data rights clearance.
Production serves the generated files from Python and does not require Node.

Local Ollama requests explicitly allocate `answer_local_context_tokens`
(default 16384, supported range 1024–32768), in addition to the character and
output budgets. The provider identity records that allocation. This avoids
depending on the runtime's hardware-dependent default; increased context uses
more memory and still needs qualification on the target host. Hosted requests
leave context allocation to their provider.

`visual_answer_model` (`GRAPHMIND_VISUAL_ANSWER_MODEL`) optionally selects a
separate retained model for requests containing image evidence. It requires
local answer mode; an empty value reuses `answer_model`. Both inputs use the
same provider, endpoint, admission lock, context/output budgets and citation
validation. Image readiness checks the configured image model independently
of text readiness. Record its actual digest with
`provider.identity(resolve=True, image=True)` when qualifying visual answers.

## Maintenance rule

For every dependency change:

1. Update the direct pin and regenerate the clean resolver snapshot.
2. Run `pip check`, offline tests, and the relevant real integration on Ubuntu and
   Windows.
3. Record model/service versions and hashes separately from Python packages.
4. Review licenses and distribution terms before release packaging.
5. Inspect the final wheel and source archive so development assets, model weights,
   credentials, and private planning files are absent.
